package authn

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"
)

// The SMTP test peer never stores or prints envelope/body contents.
func smtpPeer(t *testing.T, mode string) (string, int, <-chan struct{}) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
		reader := bufio.NewReader(conn)
		if mode == "blocked greeting" {
			_, _ = reader.ReadString('\n')
			return
		}
		fmt.Fprint(conn, "220 fixture ESMTP\r\n")
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				return
			}
			switch {
			case strings.HasPrefix(line, "EHLO"), strings.HasPrefix(line, "HELO"), strings.HasPrefix(line, "MAIL"), strings.HasPrefix(line, "RCPT"):
				fmt.Fprint(conn, "250 OK\r\n")
			case strings.HasPrefix(line, "DATA"):
				fmt.Fprint(conn, "354 Continue\r\n")
				for {
					line, err = reader.ReadString('\n')
					if err != nil {
						return
					}
					if line == ".\r\n" {
						break
					}
				}
				if mode == "lost DATA ack" {
					return
				}
				if mode == "blocked DATA ack" {
					_, _ = reader.ReadString('\n')
					return
				}
				fmt.Fprint(conn, "250 Accepted\r\n")
			case strings.HasPrefix(line, "QUIT"):
				if mode != "failed QUIT" {
					fmt.Fprint(conn, "221 Bye\r\n")
				}
				return
			default:
				return
			}
		}
	}()
	address := listener.Addr().(*net.TCPAddr)
	return "127.0.0.1", address.Port, done
}

func TestSMTPAcceptanceBoundaryAndTotalDeadline(t *testing.T) {
	for _, mode := range []string{"normal", "failed QUIT", "lost DATA ack", "blocked DATA ack", "blocked greeting"} {
		t.Run(mode, func(t *testing.T) {
			host, port, done := smtpPeer(t, mode)
			mailer, err := NewSMTPMailer(host, port, "", "", "fixture@example.test", 150*time.Millisecond)
			if err != nil {
				t.Fatal(err)
			}
			started := time.Now()
			err = mailer.SendVerification(t.Context(), "recipient@example.test", "http://example.test/verify-email?token=synthetic")
			if (err == nil) != (mode == "normal" || mode == "failed QUIT") {
				t.Fatal("SMTP acceptance boundary incorrect")
			}
			if time.Since(started) > 500*time.Millisecond {
				t.Fatal("total SMTP deadline not enforced")
			}
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("sender connection leaked")
			}
		})
	}
}

func TestSMTPCancellationClosesSocket(t *testing.T) {
	host, port, done := smtpPeer(t, "blocked greeting")
	mailer, err := NewSMTPMailer(host, port, "", "", "fixture@example.test", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 50*time.Millisecond)
	defer cancel()
	if mailer.SendVerification(ctx, "recipient@example.test", "http://example.test/verify-email") == nil {
		t.Fatal("cancelled send accepted")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancel did not close SMTP socket")
	}
}
