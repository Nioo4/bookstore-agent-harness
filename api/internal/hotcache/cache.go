package hotcache

import (
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/zeromicro/go-zero/core/collection"
	"github.com/zeromicro/go-zero/core/metric"
	"github.com/zeromicro/go-zero/core/syncx"
)

const (
	StatusDisabled = "disabled"
	StatusHit      = "hit"
	StatusMiss     = "miss"
)

var requestsTotal = metric.NewCounterVec(&metric.CounterVecOpts{
	Namespace: "bookstore",
	Subsystem: "api_hot_cache",
	Name:      "requests_total",
	Help:      "Number of hot book cache requests by result.",
	Labels:    []string{"result"},
})

type Cache struct {
	enabled bool
	ttl     time.Duration
	cache   *collection.Cache
	group   syncx.SingleFlight
	mu      sync.Mutex
	epoch   atomic.Uint64
}

type entry struct {
	value     any
	expiresAt time.Time
}

func MustNew(enabled bool, ttl time.Duration, limit int) *Cache {
	cache, err := New(enabled, ttl, limit)
	if err != nil {
		panic(err)
	}
	return cache
}

func New(enabled bool, ttl time.Duration, limit int) (*Cache, error) {
	if ttl <= 0 {
		return nil, fmt.Errorf("hot cache TTL must be positive")
	}
	if limit <= 0 {
		return nil, fmt.Errorf("hot cache limit must be positive")
	}
	local, err := collection.NewCache(ttl, collection.WithLimit(limit), collection.WithName("bookstore-hot-books"))
	if err != nil {
		return nil, err
	}
	return &Cache{enabled: enabled, ttl: ttl, cache: local, group: syncx.NewSingleFlight()}, nil
}

func (c *Cache) Get(key string, fetch func() (any, error)) (any, string, error) {
	if !c.enabled {
		requestsTotal.Inc(StatusDisabled)
		value, err := fetch()
		return value, StatusDisabled, err
	}
	if value, ok := c.getValid(key); ok {
		requestsTotal.Inc(StatusHit)
		return value, StatusHit, nil
	}

	requestsTotal.Inc(StatusMiss)
	value, err := c.group.Do(key, func() (any, error) {
		if cached, ok := c.getValid(key); ok {
			return cached, nil
		}
		loadEpoch := c.epoch.Load()
		loaded, err := fetch()
		if err != nil {
			return nil, err
		}
		c.setIfCurrent(key, loaded, loadEpoch)
		return loaded, nil
	})
	return value, StatusMiss, err
}

func (c *Cache) Del(key string) {
	if c != nil && c.cache != nil {
		c.mu.Lock()
		c.epoch.Add(1)
		c.cache.Del(key)
		c.mu.Unlock()
	}
}

func (c *Cache) getValid(key string) (any, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	raw, ok := c.cache.Get(key)
	if !ok {
		return nil, false
	}
	cached, ok := raw.(entry)
	if !ok || !time.Now().Before(cached.expiresAt) {
		c.cache.Del(key)
		return nil, false
	}
	return cached.value, true
}

func (c *Cache) setIfCurrent(key string, value any, loadEpoch uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()

	// A write invalidation that happened while fetch was in flight makes the
	// loaded value unsafe to cache. The current overlapping caller may still
	// receive it, but subsequent callers must load again.
	if c.epoch.Load() != loadEpoch {
		return
	}
	c.cache.SetWithExpire(key, entry{
		value:     value,
		expiresAt: time.Now().Add(c.ttl),
	}, c.ttl)
}

func (c *Cache) MaxStaleness() time.Duration {
	return c.ttl
}
