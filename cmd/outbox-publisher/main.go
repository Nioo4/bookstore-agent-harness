package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"bookstore/internal/outboxpublisher"
	"bookstore/internal/safelog"

	_ "github.com/go-sql-driver/mysql"
	"github.com/google/uuid"
	redis "github.com/redis/go-redis/v9"
	"github.com/zeromicro/go-zero/core/logx"
)

func main() {
	safelog.Install(logx.LogConf{Mode: "console", Level: "error"})
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	db, err := sql.Open("mysql", requiredEnv("OUTBOX_MYSQL_DSN"))
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	redisOptions, err := publisherRedisOptions(envDefault("REDIS_ADDR", "127.0.0.1:6379"))
	if err != nil {
		log.Fatal(err)
	}
	redisClient := redis.NewClient(redisOptions)
	defer redisClient.Close()

	publisher := outboxpublisher.Publisher{
		Store:  &outboxpublisher.Store{DB: db},
		Stream: outboxpublisher.RedisStream{Client: redisClient},
		Config: outboxpublisher.Config{
			OwnerID:      envDefault("OUTBOX_PUBLISHER_ID", "outbox-publisher-"+uuid.NewString()),
			Stream:       envDefault("INVENTORY_STREAM", "inventory.events"),
			PollInterval: envDuration("OUTBOX_POLL_INTERVAL", time.Second),
			BatchSize:    envInt("OUTBOX_BATCH_SIZE", 50),
			LeaseTTL:     envDuration("OUTBOX_LEASE_TTL", 30*time.Second),
			RetryBase:    envDuration("OUTBOX_RETRY_BASE", time.Second),
			RetryMax:     envDuration("OUTBOX_RETRY_MAX", 5*time.Minute),
			AlertAfter:   uint(envInt("OUTBOX_ALERT_AFTER_ATTEMPTS", 10)),
			Retention:    envDuration("BUSINESS_OUTBOX_RETENTION", 30*24*time.Hour),
		},
	}
	if len(os.Args) > 1 {
		if os.Args[1] != "replay-inventory" {
			fmt.Fprintln(os.Stderr, "usage: outbox-publisher replay-inventory --from <RFC3339 UTC> --to <RFC3339 UTC> [--dry-run]")
			os.Exit(2)
		}
		from, to, dryRun := parseReplayFlags(os.Args[2:])
		count, err := publisher.ReplayInventory(ctx, from, to, dryRun)
		if err != nil {
			log.Fatal(err)
		}
		fmt.Fprintf(os.Stdout, "inventory events selected=%d dry_run=%t\n", count, dryRun)
		return
	}
	if err := publisher.Run(ctx); err != nil && err != context.Canceled {
		log.Fatal(err)
	}
}

func publisherRedisOptions(value string) (*redis.Options, error) {
	if strings.HasPrefix(value, "redis://") || strings.HasPrefix(value, "rediss://") {
		return redis.ParseURL(value)
	}
	return &redis.Options{
		Addr:     value,
		Password: os.Getenv("REDIS_PASSWORD"),
		DB:       envInt("REDIS_DB", 0),
	}, nil
}

func parseReplayFlags(args []string) (time.Time, time.Time, bool) {
	flags := flag.NewFlagSet("replay-inventory", flag.ContinueOnError)
	flags.SetOutput(io.Discard) // Invalid raw arguments must not enter logs.
	fromText := flags.String("from", "", "inclusive RFC3339 UTC start")
	toText := flags.String("to", "", "exclusive RFC3339 UTC end")
	dryRun := flags.Bool("dry-run", false, "count without publishing")
	if err := flags.Parse(args); err != nil || *fromText == "" || *toText == "" || flags.NArg() != 0 {
		log.Fatal("replay-inventory requires --from and --to and accepts only --dry-run")
	}
	from, err := time.Parse(time.RFC3339, *fromText)
	if err != nil {
		log.Fatalf("invalid --from: %v", err)
	}
	to, err := time.Parse(time.RFC3339, *toText)
	if err != nil {
		log.Fatalf("invalid --to: %v", err)
	}
	return from, to, *dryRun
}

func requiredEnv(name string) string {
	value := os.Getenv(name)
	if value == "" {
		log.Fatalf("%s is required", name)
	}
	return value
}

func envDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil && strings.HasSuffix(value, "d") {
		days, daysErr := strconv.Atoi(strings.TrimSuffix(value, "d"))
		if daysErr == nil && days > 0 {
			return time.Duration(days) * 24 * time.Hour
		}
	}
	if err != nil {
		log.Fatalf("invalid %s: %v", name, err)
	}
	return parsed
}

func envInt(name string, fallback int) int {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		log.Fatalf("invalid %s: %v", name, err)
	}
	return parsed
}
