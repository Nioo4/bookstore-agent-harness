package authn

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

type User struct {
	UserID          int64        `db:"user_id"`
	Email           string       `db:"email"`
	DisplayName     string       `db:"display_name"`
	PasswordHash    string       `db:"password_hash"`
	EmailVerifiedAt sql.NullTime `db:"email_verified_at"`
	Status          string       `db:"status"`
}

type Store interface {
	Register(ctx context.Context, email, displayName, passwordHash string, tokenDigest []byte, expiresAt time.Time) (int64, error)
	VerifyEmail(ctx context.Context, tokenDigest []byte, now time.Time) (int64, error)
	FindByEmail(ctx context.Context, email string) (*User, error)
	FindByID(ctx context.Context, userID int64) (*User, error)
	CreateVerification(ctx context.Context, userID int64, tokenDigest []byte, now time.Time) (*User, bool, error)
}

type SQLStore struct{ conn sqlx.SqlConn }

func NewSQLStore(conn sqlx.SqlConn) *SQLStore { return &SQLStore{conn: conn} }

func (s *SQLStore) Register(ctx context.Context, email, displayName, passwordHash string, tokenDigest []byte, expiresAt time.Time) (int64, error) {
	var userID int64
	err := s.conn.TransactCtx(ctx, func(ctx context.Context, session sqlx.Session) error {
		result, err := session.ExecCtx(ctx, `insert into user_account
			(email, display_name, password_hash, status) values (?, ?, ?, 'ACTIVE')`,
			email, displayName, passwordHash)
		if err != nil {
			var mysqlErr *mysql.MySQLError
			if errors.As(err, &mysqlErr) && mysqlErr.Number == 1062 {
				return ErrEmailExists
			}
			return err
		}
		userID, err = result.LastInsertId()
		if err != nil {
			return err
		}
		_, err = session.ExecCtx(ctx, `insert into email_verification_token
			(token_digest, user_id, expires_at, created_at) values (?, ?, ?, ?)`, tokenDigest, userID, expiresAt, expiresAt.Add(-verificationTTL))
		return err
	})
	return userID, err
}

func (s *SQLStore) VerifyEmail(ctx context.Context, tokenDigest []byte, now time.Time) (int64, error) {
	var userID int64
	// Locate without a lock, then always acquire account before token. Two
	// different links for the same account must serialize on the same row.
	if err := s.conn.QueryRowCtx(ctx, &userID, "select user_id from email_verification_token where token_digest = ?", tokenDigest); err != nil {
		if errors.Is(err, sqlx.ErrNotFound) {
			return 0, ErrVerificationInvalid
		}
		return 0, ErrDependencyUnavailable
	}
	err := s.conn.TransactCtx(ctx, func(ctx context.Context, session sqlx.Session) error {
		user, err := lockedUser(ctx, session, userID)
		if errors.Is(err, ErrAuthRequired) {
			return ErrVerificationInvalid
		}
		if err != nil {
			return err
		}
		var record struct {
			UserID    int64        `db:"user_id"`
			ExpiresAt time.Time    `db:"expires_at"`
			UsedAt    sql.NullTime `db:"used_at"`
		}
		if err := session.QueryRowCtx(ctx, &record,
			"select user_id, expires_at, used_at from email_verification_token where token_digest = ? for update", tokenDigest); err != nil {
			if errors.Is(err, sqlx.ErrNotFound) {
				return ErrVerificationInvalid
			}
			return err
		}
		if record.UserID != userID {
			return ErrVerificationInvalid
		}
		if record.UsedAt.Valid || user.EmailVerifiedAt.Valid {
			return ErrVerificationUsed
		}
		if !record.ExpiresAt.After(now) {
			return ErrVerificationExpired
		}
		result, err := session.ExecCtx(ctx, `update user_account
			set email_verified_at = ? where user_id = ? and status = 'ACTIVE' and email_verified_at is null`, now, userID)
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if affected != 1 {
			return ErrVerificationUsed
		}
		result, err = session.ExecCtx(ctx, `update email_verification_token
			set used_at = ? where token_digest = ? and user_id = ? and used_at is null`, now, tokenDigest, userID)
		if err != nil {
			return err
		}
		affected, err = result.RowsAffected()
		if err != nil {
			return err
		}
		if affected != 1 {
			return ErrVerificationUsed
		}
		return nil
	})
	if err != nil && !errors.Is(err, ErrVerificationInvalid) && !errors.Is(err, ErrVerificationUsed) && !errors.Is(err, ErrVerificationExpired) {
		err = ErrDependencyUnavailable
	}
	return userID, err
}

const userColumns = "select user_id, email, display_name, password_hash, email_verified_at, status from user_account where user_id = ?"

func lockedUser(ctx context.Context, session sqlx.Session, userID int64) (*User, error) {
	var user User
	err := session.QueryRowCtx(ctx, &user, userColumns+" for update", userID)
	if errors.Is(err, sqlx.ErrNotFound) {
		return nil, ErrAuthRequired
	}
	if err != nil {
		return nil, ErrDependencyUnavailable
	}
	if user.Status != "ACTIVE" {
		return nil, ErrAuthRequired
	}
	return &user, nil
}

func (s *SQLStore) FindByID(ctx context.Context, userID int64) (*User, error) {
	var user User
	err := s.conn.QueryRowCtx(ctx, &user, userColumns, userID)
	if errors.Is(err, sqlx.ErrNotFound) {
		return nil, ErrAuthRequired
	}
	if err != nil {
		return nil, ErrDependencyUnavailable
	}
	if user.Status != "ACTIVE" {
		return nil, ErrAuthRequired
	}
	return &user, nil
}

// CreateVerification returns only after a confirmed commit. A failed/uncertain
// commit is not permission to send. Old tokens retain their original lifetime.
func (s *SQLStore) CreateVerification(ctx context.Context, userID int64, tokenDigest []byte, now time.Time) (*User, bool, error) {
	var user *User
	created := false
	err := s.conn.TransactCtx(ctx, func(ctx context.Context, session sqlx.Session) error {
		var err error
		user, err = lockedUser(ctx, session, userID)
		if err != nil || user.EmailVerifiedAt.Valid {
			return err
		}
		_, err = session.ExecCtx(ctx, `insert into email_verification_token
			(token_digest, user_id, expires_at, created_at) values (?, ?, ?, ?)`, tokenDigest, userID, now.Add(verificationTTL), now)
		created = err == nil
		return err
	})
	if err != nil {
		if errors.Is(err, ErrAuthRequired) {
			return nil, false, ErrAuthRequired
		}
		return nil, false, ErrDependencyUnavailable
	}
	return user, created, nil
}

func (s *SQLStore) FindByEmail(ctx context.Context, email string) (*User, error) {
	var user User
	err := s.conn.QueryRowCtx(ctx, &user, `select user_id, email, display_name, password_hash,
		email_verified_at, status from user_account where email = ? limit 1`, email)
	if errors.Is(err, sqlx.ErrNotFound) {
		return nil, ErrInvalidCredentials
	}
	if err != nil {
		return nil, err
	}
	if user.Status != "ACTIVE" {
		return nil, ErrInvalidCredentials
	}
	return &user, nil
}
