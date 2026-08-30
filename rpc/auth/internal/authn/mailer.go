package authn

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"net/smtp"
	"strings"
	"time"
)

type Mailer interface {
	SendVerification(ctx context.Context, recipient, verificationURL string) error
}

type SMTPMailer struct {
	host, address, username, password, from string
	timeout                                 time.Duration
}

func NewSMTPMailer(host string, port int, username, password, from string, timeout time.Duration) (*SMTPMailer, error) {
	if strings.TrimSpace(host) == "" || port <= 0 || port > 65535 || strings.TrimSpace(from) == "" || strings.ContainsAny(from, "\r\n") || timeout <= 0 {
		return nil, fmt.Errorf("SMTP host, port and from address are required")
	}
	return &SMTPMailer{
		host: host, address: net.JoinHostPort(host, fmt.Sprint(port)),
		username: username, password: password, from: from, timeout: timeout,
	}, nil
}

func (m *SMTPMailer) SendVerification(ctx context.Context, recipient, verificationURL string) error {
	ctx, cancel := context.WithTimeout(ctx, m.timeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}
	// Never return server replies: they can echo recipient, credentials or body.
	unconfirmed := errors.New("verification email delivery unconfirmed")
	if strings.ContainsAny(recipient+verificationURL, "\r\n") {
		return unconfirmed
	}
	conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", m.address)
	if err != nil {
		return unconfirmed
	}
	defer conn.Close()
	deadline, _ := ctx.Deadline()
	if err := conn.SetDeadline(deadline); err != nil {
		return unconfirmed
	}
	// Closing the real socket interrupts TLS, AUTH and DATA even when the
	// caller cancels before the deadline. No detached sender goroutine exists.
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	client, err := smtp.NewClient(conn, m.host)
	if err != nil {
		return unconfirmed
	}
	defer client.Close()
	if ok, _ := client.Extension("STARTTLS"); ok {
		if err := client.StartTLS(&tls.Config{ServerName: m.host, MinVersion: tls.VersionTLS12}); err != nil {
			return unconfirmed
		}
	}
	if m.username != "" {
		if err := client.Auth(smtp.PlainAuth("", m.username, m.password, m.host)); err != nil {
			return unconfirmed
		}
	}
	if err := client.Mail(m.from); err != nil {
		return unconfirmed
	}
	if err := client.Rcpt(recipient); err != nil {
		return unconfirmed
	}
	writer, err := client.Data()
	if err != nil {
		return unconfirmed
	}
	body := "From: " + m.from + "\r\n" +
		"To: " + recipient + "\r\n" +
		"Subject: Verify your Bookstore account\r\n" +
		"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n" +
		"Open this link within 24 hours to verify your email:\r\n" + verificationURL + "\r\n"
	if _, err := writer.Write([]byte(body)); err != nil {
		return unconfirmed
	}
	if err := writer.Close(); err != nil {
		return unconfirmed
	}
	// Close() above read the final DATA acceptance. QUIT/close failure cannot
	// undo acceptance and must not cause a duplicate application retry.
	_ = client.Quit()
	return nil
}
