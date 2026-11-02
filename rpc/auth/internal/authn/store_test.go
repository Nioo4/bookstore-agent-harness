package authn

import (
	"errors"
	"regexp"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

func newSQLStoreTest(t *testing.T) (*SQLStore, sqlmock.Sqlmock) {
	t.Helper()
	db, mock, err := sqlmock.New()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return NewSQLStore(sqlx.NewSqlConnFromDB(db)), mock
}

func TestSQLStoreRegisterMapsDuplicateEmail(t *testing.T) {
	store, mock := newSQLStoreTest(t)
	mock.ExpectBegin()
	mock.ExpectExec(regexp.QuoteMeta(`insert into user_account
			(email, display_name, password_hash, status) values (?, ?, ?, 'ACTIVE')`)).
		WithArgs("user@example.com", "Alice", "hash").
		WillReturnError(&mysql.MySQLError{Number: 1062, Message: "duplicate email"})
	mock.ExpectRollback()
	_, err := store.Register(t.Context(), "user@example.com", "Alice", "hash", make([]byte, 32), time.Now())
	if !errors.Is(err, ErrEmailExists) {
		t.Fatalf("error=%v; want ErrEmailExists", err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestSQLStoreVerificationTokenIsSingleUseAndExpires(t *testing.T) {
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name      string
		expiresAt time.Time
		usedAt    any
		want      error
	}{
		{name: "expired", expiresAt: now.Add(-time.Second), usedAt: nil, want: ErrVerificationExpired},
		{name: "exact expiration", expiresAt: now, usedAt: nil, want: ErrVerificationExpired},
		{name: "used", expiresAt: now.Add(time.Hour), usedAt: now.Add(-time.Minute), want: ErrVerificationUsed},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store, mock := newSQLStoreTest(t)
			mock.ExpectQuery("select user_id from email_verification_token").WillReturnRows(sqlmock.NewRows([]string{"user_id"}).AddRow(7))
			mock.ExpectBegin()
			expectLockedUser(mock, "ACTIVE", nil)
			mock.ExpectQuery(regexp.QuoteMeta("select user_id, expires_at, used_at from email_verification_token where token_digest = ? for update")).
				WithArgs(sqlmock.AnyArg()).
				WillReturnRows(sqlmock.NewRows([]string{"user_id", "expires_at", "used_at"}).AddRow(7, tt.expiresAt, tt.usedAt))
			mock.ExpectRollback()
			_, err := store.VerifyEmail(t.Context(), make([]byte, 32), now)
			if !errors.Is(err, tt.want) {
				t.Fatalf("error=%v; want %v", err, tt.want)
			}
			if err := mock.ExpectationsWereMet(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestSQLStoreVerificationAtomicallyConsumesTokenAndVerifiesUser(t *testing.T) {
	store, mock := newSQLStoreTest(t)
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	digest := make([]byte, 32)
	mock.ExpectQuery("select user_id from email_verification_token").WithArgs(digest).WillReturnRows(sqlmock.NewRows([]string{"user_id"}).AddRow(7))
	mock.ExpectBegin()
	expectLockedUser(mock, "ACTIVE", nil)
	mock.ExpectQuery(regexp.QuoteMeta("select user_id, expires_at, used_at from email_verification_token where token_digest = ? for update")).
		WithArgs(digest).
		WillReturnRows(sqlmock.NewRows([]string{"user_id", "expires_at", "used_at"}).AddRow(7, now.Add(time.Second), nil))
	mock.ExpectExec(regexp.QuoteMeta(`update user_account
			set email_verified_at = ? where user_id = ? and status = 'ACTIVE' and email_verified_at is null`)).
		WithArgs(now, int64(7)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec(regexp.QuoteMeta(`update email_verification_token
			set used_at = ? where token_digest = ? and user_id = ? and used_at is null`)).
		WithArgs(now, digest, int64(7)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	userID, err := store.VerifyEmail(t.Context(), digest, now)
	if err != nil || userID != 7 {
		t.Fatalf("userID=%d err=%v", userID, err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func expectLockedUser(mock sqlmock.Sqlmock, status string, verified any) {
	mock.ExpectQuery(regexp.QuoteMeta(userColumns + " for update")).WithArgs(int64(7)).WillReturnRows(
		sqlmock.NewRows([]string{"user_id", "email", "display_name", "password_hash", "email_verified_at", "status"}).AddRow(7, "unit@example.test", "Unit", "hash", verified, status))
}

func TestResendStoreCommitFailureDoesNotGrantSend(t *testing.T) {
	store, mock := newSQLStoreTest(t)
	now := time.Date(2026, 8, 27, 0, 0, 0, 0, time.UTC)
	mock.ExpectBegin()
	expectLockedUser(mock, "ACTIVE", nil)
	mock.ExpectExec("insert into email_verification_token").WithArgs(sqlmock.AnyArg(), int64(7), now.Add(24*time.Hour), now).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit().WillReturnError(errors.New("synthetic uncertain commit"))
	user, created, err := store.CreateVerification(t.Context(), 7, make([]byte, 32), now)
	if user != nil || created || !errors.Is(err, ErrDependencyUnavailable) {
		t.Fatal("uncertain commit incorrectly authorized send")
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestVerificationAffectedRowsFailureRollsBack(t *testing.T) {
	store, mock := newSQLStoreTest(t)
	now := time.Date(2026, 8, 27, 0, 0, 0, 0, time.UTC)
	mock.ExpectQuery("select user_id from email_verification_token").WillReturnRows(sqlmock.NewRows([]string{"user_id"}).AddRow(7))
	mock.ExpectBegin()
	expectLockedUser(mock, "ACTIVE", nil)
	mock.ExpectQuery("select user_id, expires_at, used_at from email_verification_token").WillReturnRows(sqlmock.NewRows([]string{"user_id", "expires_at", "used_at"}).AddRow(7, now.Add(time.Hour), nil))
	mock.ExpectExec("update user_account").WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectRollback()
	if _, err := store.VerifyEmail(t.Context(), make([]byte, 32), now); !errors.Is(err, ErrVerificationUsed) {
		t.Fatal("zero-row update did not roll back")
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}
