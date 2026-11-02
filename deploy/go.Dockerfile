# Keep a non-.go extension so go test ./... does not parse this Dockerfile.
FROM golang:1.26.1-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY api/ ./api/
COPY rpc/ ./rpc/
COPY internal/ ./internal/
COPY cmd/ ./cmd/
RUN CGO_ENABLED=0 go build -trimpath -o /out/bookstore-service ./cmd/bookstore-service && \
    CGO_ENABLED=0 go build -trimpath -o /out/bookstore-migrate ./cmd/bookstore-migrate

FROM alpine:3.22
RUN apk add --no-cache ca-certificates curl netcat-openbsd && addgroup -g 10001 bookstore && adduser -D -u 10001 -G bookstore bookstore
WORKDIR /app
COPY --from=build /out/ /usr/local/bin/
COPY migrations/ ./migrations/
COPY deploy/config/ ./config/
USER 10001:10001
CMD ["bookstore-service"]
