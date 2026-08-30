// Test-only v2 adapter. Uses the production RPC server/Service/SQLStore, binds
// loopback only, and never migrates, resets or fabricates database outcomes.
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"database/sql"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"bookstore/rpc/auth/jwtauth"
	"bookstore/rpc/order/internal/ordering"
	"bookstore/rpc/order/internal/server"
	"bookstore/rpc/order/internal/svc"
	orderpb "bookstore/rpc/order/order"

	"github.com/go-sql-driver/mysql"
	"github.com/golang-jwt/jwt/v4"
	redis "github.com/redis/go-redis/v9"
	"github.com/zeromicro/go-zero/core/stores/sqlx"
	"google.golang.org/grpc"
)

type configuration struct {
	Host       string `json:"host"`
	Port       int    `json:"port"`
	User       string `json:"user"`
	Password   string `json:"password"`
	Database   string `json:"database"`
	ServerUUID string `json:"server_uuid"`
	RedisURL   string `json:"redis_url"`
	RedisRunID string `json:"redis_run_id"`
}

type clockStore struct {
	ordering.Store
	db     *sql.DB
	expire *atomic.Bool
}

func (s *clockStore) Decide(ctx context.Context, userID int64, conversation string, digest []byte, decision string, now time.Time) (*ordering.ConfirmationResult, error) {
	if s.expire.Load() {
		// Inject the exact persisted expiry at the existing SQLStore clock boundary;
		// no expiry-row rewrite or ten-minute wall-clock sleep is needed.
		if err := s.db.QueryRowContext(ctx, "SELECT expires_at FROM pending_action WHERE token_hash=?", digest).Scan(&now); err != nil {
			return nil, err
		}
	}
	return s.Store.Decide(ctx, userID, conversation, digest, decision, now)
}

func run() error {
	decoder := json.NewDecoder(os.Stdin)
	var c configuration
	if err := decoder.Decode(&c); err != nil {
		return err
	}
	if c.Database != "bookstore_eval_v2_test" || c.ServerUUID == "" || len(c.RedisRunID) != 40 {
		return fmt.Errorf("isolated identities required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	redisOptions, err := redis.ParseURL(c.RedisURL)
	if err != nil {
		return err
	}
	r := redis.NewClient(redisOptions)
	defer r.Close()
	info, err := r.Info(ctx, "server").Result()
	if err != nil || !strings.Contains(info, "run_id:"+c.RedisRunID+"\r\n") {
		return fmt.Errorf("Redis identity mismatch")
	}
	if redisOptions.Username != "" {
		return fmt.Errorf("test adapter expects dedicated default-user Redis")
	}
	cache, err := ordering.NewRedisCacheInvalidator(redisOptions.Addr, redisOptions.Password, redisOptions.DB)
	if err != nil {
		return err
	}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	der, err := x509.MarshalPKIXPublicKey(public)
	if err != nil {
		return err
	}
	publicPEM := string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
	const issuer = "bookstore-eval-v2"
	verifier := jwtauth.NewVerifier(public, issuer)
	tokens := make(map[string]string)
	for id := 1; id <= 3; id++ {
		claims := jwt.MapClaims{"sub": strconv.Itoa(id), "iss": issuer, "iat": time.Now().Unix(), "exp": time.Now().Add(8 * time.Hour).Unix(),
			"email": fmt.Sprintf("v2-u%d@example.test", id), "email_verified": id != 3}
		tokens[strconv.Itoa(id)], err = jwt.NewWithClaims(jwt.SigningMethodEdDSA, claims).SignedString(private)
		if err != nil {
			return err
		}
	}
	var expire atomic.Bool
	var addresses []string
	var connectionIDs []uint64
	for index := 0; index < 2; index++ {
		cfg := mysql.NewConfig()
		cfg.User, cfg.Passwd, cfg.DBName = c.User, c.Password, c.Database
		cfg.Net, cfg.Addr = "tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.Port))
		cfg.ParseTime, cfg.Loc = true, time.UTC
		cfg.Params = map[string]string{"charset": "utf8mb4"}
		db, err := sql.Open("mysql", cfg.FormatDSN())
		if err != nil {
			return err
		}
		defer db.Close()
		db.SetMaxOpenConns(1)
		db.SetMaxIdleConns(1)
		var database, serverUUID string
		var connectionID uint64
		if err = db.QueryRowContext(ctx, "SELECT DATABASE(),@@server_uuid,CONNECTION_ID()").Scan(&database, &serverUUID, &connectionID); err != nil {
			return err
		}
		if database != c.Database || serverUUID != c.ServerUUID {
			return fmt.Errorf("MySQL identity mismatch")
		}
		connectionIDs = append(connectionIDs, connectionID)
		store := &clockStore{Store: ordering.NewSQLStore(sqlx.NewSqlConnFromDB(db)), db: db, expire: &expire}
		service, err := ordering.NewService(store, verifier, cache, 10*time.Minute)
		if err != nil {
			return err
		}
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return err
		}
		defer listener.Close()
		rpc := grpc.NewServer()
		defer rpc.Stop()
		orderpb.RegisterOrderServiceServer(rpc, server.NewOrderServer(&svc.ServiceContext{Orders: service}))
		go rpc.Serve(listener)
		addresses = append(addresses, listener.Addr().String())
	}
	// The bootstrap object contains temporary JWTs: parent consumes it in memory
	// and records only endpoints/connection IDs/binary digest in evidence.
	boot, _ := json.Marshal(map[string]any{"addresses": addresses, "connection_ids": connectionIDs, "server_uuid": c.ServerUUID,
		"public_key": publicPEM, "issuer": issuer, "tokens": tokens})
	fmt.Println("V2_BOOT=" + string(boot))
	for {
		var command struct {
			Command string `json:"command"`
			Enabled bool   `json:"enabled"`
		}
		if err := decoder.Decode(&command); err != nil {
			return nil
		}
		switch command.Command {
		case "stop":
			return nil
		case "expiry_boundary":
			expire.Store(command.Enabled)
			fmt.Println("V2_CONTROL=ok")
		default:
			return fmt.Errorf("unknown fixed command")
		}
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "v2 isolated order helper failed")
		os.Exit(3)
	}
}
