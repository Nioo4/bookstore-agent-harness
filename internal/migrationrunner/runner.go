package migrationrunner

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	drivermysql "github.com/go-sql-driver/mysql"
	migrate "github.com/golang-migrate/migrate/v4"
	migratemysql "github.com/golang-migrate/migrate/v4/database/mysql"
	_ "github.com/golang-migrate/migrate/v4/source/file"
)

const RecoveryDocument = "docs/migration-recovery.md"

type Config struct {
	DataSource        string
	MigrationsPath    string
	ConfirmationV5SQL string
}

type migrationState struct {
	Exists  bool
	Version uint
	Dirty   bool
}

func Run(ctx context.Context, config Config) error {
	if strings.TrimSpace(config.DataSource) == "" {
		return errors.New("BOOKSTORE_MYSQL_DSN is required")
	}
	if strings.TrimSpace(config.MigrationsPath) == "" {
		config.MigrationsPath = "migrations"
	}
	if strings.TrimSpace(config.ConfirmationV5SQL) == "" {
		config.ConfirmationV5SQL = filepath.Join(config.MigrationsPath, "verify_confirmation_v5.sql")
	}

	dsnConfig, err := drivermysql.ParseDSN(config.DataSource)
	if err != nil {
		return errors.New("BOOKSTORE_MYSQL_DSN is invalid")
	}
	if dsnConfig.DBName == "" {
		return errors.New("BOOKSTORE_MYSQL_DSN must select a database")
	}
	dsnConfig.MultiStatements = true
	dsnConfig.ParseTime = true
	db, err := sql.Open("mysql", dsnConfig.FormatDSN())
	if err != nil {
		return errors.New("open migration database failed")
	}
	defer db.Close()
	if err := db.PingContext(ctx); err != nil {
		return errors.New("connect to migration database failed")
	}

	state, err := inspectState(ctx, db)
	if err != nil {
		return fmt.Errorf("inspect migration state failed; see %s: %w", RecoveryDocument, err)
	}
	preflight, err := migrationPreflight(state)
	if err != nil {
		return err
	}
	if preflight {
		if err := verifyConfirmationV5(ctx, db, config.ConfirmationV5SQL); err != nil {
			return fmt.Errorf("confirmation v5 schema preflight failed; see %s: %w", RecoveryDocument, err)
		}
	}

	databaseDriver, err := migratemysql.WithInstance(db, &migratemysql.Config{DatabaseName: dsnConfig.DBName})
	if err != nil {
		return errors.New("initialize migration database driver failed")
	}
	sourceURL, err := fileSourceURL(config.MigrationsPath)
	if err != nil {
		return fmt.Errorf("resolve migrations path: %w", err)
	}
	migrator, err := migrate.NewWithDatabaseInstance(sourceURL, "mysql", databaseDriver)
	if err != nil {
		return fmt.Errorf("initialize migration source: %w", err)
	}
	upErr := migrator.Up()
	sourceCloseErr, databaseCloseErr := migrator.Close()
	if upErr != nil && !errors.Is(upErr, migrate.ErrNoChange) {
		return fmt.Errorf("apply migrations failed; see %s: %w", RecoveryDocument, upErr)
	}
	if sourceCloseErr != nil {
		return fmt.Errorf("close migration source: %w", sourceCloseErr)
	}
	if databaseCloseErr != nil {
		return fmt.Errorf("close migration database driver: %w", databaseCloseErr)
	}
	return nil
}

func inspectState(ctx context.Context, db *sql.DB) (migrationState, error) {
	var tableCount int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
		WHERE table_schema=DATABASE() AND table_name='schema_migrations'`).Scan(&tableCount); err != nil {
		return migrationState{}, err
	}
	if tableCount == 0 {
		return migrationState{}, nil
	}
	var state migrationState
	err := db.QueryRowContext(ctx, `SELECT version, dirty FROM schema_migrations LIMIT 1`).Scan(&state.Version, &state.Dirty)
	if errors.Is(err, sql.ErrNoRows) {
		return migrationState{}, nil
	}
	if err != nil {
		return migrationState{}, err
	}
	state.Exists = true
	return state, nil
}

func migrationPreflight(state migrationState) (bool, error) {
	if state.Exists && state.Dirty {
		return false, fmt.Errorf("migration database is dirty at version %d; automatic recovery is forbidden; see %s",
			state.Version, RecoveryDocument)
	}
	return state.Exists && state.Version == 5, nil
}

func verifyConfirmationV5(ctx context.Context, db *sql.DB, verificationPath string) error {
	contents, err := os.ReadFile(verificationPath)
	if err != nil {
		return fmt.Errorf("read confirmation v5 verification SQL: %w", err)
	}
	connection, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer connection.Close()
	_, err = connection.ExecContext(ctx, string(contents))
	return err
}

func fileSourceURL(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	slashPath := filepath.ToSlash(absolute)
	if volume := filepath.ToSlash(filepath.VolumeName(absolute)); volume != "" {
		pathWithoutVolume := strings.TrimPrefix(slashPath, volume)
		return (&url.URL{Scheme: "file", Host: volume, Path: pathWithoutVolume}).String(), nil
	}
	return (&url.URL{Scheme: "file", Path: slashPath}).String(), nil
}
