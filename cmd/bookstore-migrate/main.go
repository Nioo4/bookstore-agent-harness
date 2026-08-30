package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"bookstore/internal/migrationrunner"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	migrationsPath := getenvDefault("BOOKSTORE_MIGRATIONS_PATH", "migrations")
	err := migrationrunner.Run(ctx, migrationrunner.Config{
		DataSource:     os.Getenv("BOOKSTORE_MYSQL_DSN"),
		MigrationsPath: migrationsPath,
		ConfirmationV5SQL: getenvDefault("BOOKSTORE_CONFIRMATION_V5_VERIFY_PATH",
			filepath.Join(migrationsPath, "verify_confirmation_v5.sql")),
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "migration startup failed:", err)
		os.Exit(1)
	}
	fmt.Fprintln(os.Stdout, "database migrations are current")
}

func getenvDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
