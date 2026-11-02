package authn

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
)

func TestMySQLVerificationLockWaitCancellation(t *testing.T) {
	store, db := authMySQL(t)
	now := time.Date(2026, 8, 27, 12, 0, 0, 0, time.UTC)
	id, old := newVerificationAccount(t, store, db, now)
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal("test transaction failed")
	}
	defer tx.Rollback()
	var locked int64
	if err := tx.QueryRowContext(t.Context(), "select user_id from user_account where user_id = ? for update", id).Scan(&locked); err != nil {
		t.Fatal("test row lock failed")
	}
	_, next, _ := GenerateVerificationToken()
	for _, verify := range []bool{false, true} {
		ctx, cancel := context.WithTimeout(t.Context(), 100*time.Millisecond)
		started := time.Now()
		var err error
		if verify {
			_, err = store.VerifyEmail(ctx, old, now)
		} else {
			_, _, err = store.CreateVerification(ctx, id, next[:], now)
		}
		cancel()
		if !errors.Is(err, ErrDependencyUnavailable) || time.Since(started) > time.Second {
			t.Fatal("lock wait not cancelled as dependency failure")
		}
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal("test lock release failed")
	}
	var tokens, consumed int
	if err := db.QueryRow("select count(*), sum(used_at is not null) from email_verification_token where user_id = ?", id).Scan(&tokens, &consumed); err != nil || tokens != 1 || consumed != 0 {
		t.Fatal("cancelled lock waiter mutated token state")
	}
	user, err := store.FindByID(t.Context(), id)
	if err != nil || user.EmailVerifiedAt.Valid {
		t.Fatal("cancelled lock waiter verified account")
	}
}

func authMySQL(t *testing.T) (*SQLStore, *sql.DB) {
	t.Helper()
	dsn := os.Getenv("AUTH_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("AUTH_TEST_MYSQL_DSN required (migrated isolated test database)")
	}
	config, err := mysql.ParseDSN(dsn)
	if err != nil || !strings.HasSuffix(config.DBName, "_test") || !strings.HasPrefix(config.Addr, "127.0.0.1:") {
		t.Fatal("isolated local test database required")
	}
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatal("test MySQL open failed")
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := db.PingContext(t.Context()); err != nil {
		t.Fatal("test MySQL unavailable")
	}
	return NewSQLStore(sqlx.NewSqlConnFromDB(db)), db
}

func newVerificationAccount(t *testing.T, store *SQLStore, db *sql.DB, now time.Time) (int64, []byte) {
	t.Helper()
	_, digest, err := GenerateVerificationToken()
	if err != nil {
		t.Fatal("test randomness failed")
	}
	email := fmt.Sprintf("dq009-%d@example.test", time.Now().UnixNano())
	id, err := store.Register(t.Context(), email, "DQ009 fixture", "unused-fixture-hash", digest[:], now.Add(verificationTTL))
	if err != nil {
		t.Fatal("test account insert failed")
	}
	t.Cleanup(func() {
		// Only this newly inserted fixture account, no table resets/migrations.
		_, _ = db.Exec("delete from email_verification_token where user_id = ?", id)
		_, _ = db.Exec("delete from user_account where user_id = ?", id)
	})
	return id, digest[:]
}

func TestMySQLVerificationAccountLevelSingleUse(t *testing.T) {
	store, db := authMySQL(t)
	now := time.Date(2026, 8, 27, 12, 0, 0, 0, time.UTC)
	for _, same := range []bool{true, false} {
		t.Run(fmt.Sprint("same-token=", same), func(t *testing.T) {
			id, old := newVerificationAccount(t, store, db, now)
			_, next, _ := GenerateVerificationToken()
			if _, created, err := store.CreateVerification(t.Context(), id, next[:], now); err != nil || !created {
				t.Fatal("resend insert failed")
			}
			tokens := [][]byte{old, next[:]}
			if same {
				tokens[1] = old
			}
			start, results := make(chan struct{}), make(chan error, 2)
			for _, token := range tokens {
				go func(token []byte) {
					<-start
					_, err := store.VerifyEmail(t.Context(), token, now.Add(time.Hour))
					results <- err
				}(token)
			}
			close(start)
			first, second := <-results, <-results
			if !((first == nil && errors.Is(second, ErrVerificationUsed)) || (second == nil && errors.Is(first, ErrVerificationUsed))) {
				t.Fatal("exactly one account verification must succeed")
			}
			var consumed int
			if err := db.QueryRow("select count(*) from email_verification_token where user_id = ? and used_at is not null", id).Scan(&consumed); err != nil || consumed != 1 {
				t.Fatal("wrong consumed token count")
			}
			user, err := store.FindByID(t.Context(), id)
			if err != nil || !user.EmailVerifiedAt.Time.Equal(now.Add(time.Hour)) {
				t.Fatal("verification timestamp changed")
			}
			if _, err := store.VerifyEmail(t.Context(), next[:], now.Add(25*time.Hour)); !errors.Is(err, ErrVerificationUsed) {
				t.Fatal("already verified must precede expiration")
			}
		})
	}
}

func TestMySQLVerificationDisabledExpiryAndResendRace(t *testing.T) {
	store, db := authMySQL(t)
	now := time.Date(2026, 8, 27, 12, 0, 0, 0, time.UTC)
	id, old := newVerificationAccount(t, store, db, now)
	if _, err := store.VerifyEmail(t.Context(), old, now.Add(verificationTTL)); !errors.Is(err, ErrVerificationExpired) {
		t.Fatal("24h boundary must be expired")
	}
	if _, err := db.Exec("update user_account set status = 'DISABLED' where user_id = ?", id); err != nil {
		t.Fatal("fixture disable failed")
	}
	if _, err := store.VerifyEmail(t.Context(), old, now.Add(verificationTTL)); !errors.Is(err, ErrVerificationInvalid) {
		t.Fatal("disabled account must precede expiration")
	}
	if _, err := store.FindByID(t.Context(), id); !errors.Is(err, ErrAuthRequired) {
		t.Fatal("disabled resend identity not rejected")
	}
	if _, _, err := store.CreateVerification(t.Context(), id, old, now); !errors.Is(err, ErrAuthRequired) {
		t.Fatal("disabled account not rechecked under lock")
	}
	for range 10 {
		id, old := newVerificationAccount(t, store, db, now)
		_, next, _ := GenerateVerificationToken()
		start, results := make(chan struct{}), make(chan error, 2)
		go func() { <-start; _, _, err := store.CreateVerification(t.Context(), id, next[:], now); results <- err }()
		go func() { <-start; _, err := store.VerifyEmail(t.Context(), old, now); results <- err }()
		close(start)
		if <-results != nil || <-results != nil {
			t.Fatal("resend/verification race failed or deadlocked")
		}
		if _, created, err := store.CreateVerification(t.Context(), id, next[:], now); err != nil || created {
			t.Fatal("verified account created another token")
		}
	}
}
