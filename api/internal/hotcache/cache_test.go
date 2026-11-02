package hotcache

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestCacheHitAvoidsSecondFetch(t *testing.T) {
	cache, err := New(true, time.Second, 10)
	if err != nil {
		t.Fatal(err)
	}
	var calls int
	fetch := func() (any, error) {
		calls++
		return "book", nil
	}

	first, firstStatus, err := cache.Get("1", fetch)
	if err != nil || first != "book" || firstStatus != StatusMiss {
		t.Fatalf("first = %v/%s, err=%v", first, firstStatus, err)
	}
	second, secondStatus, err := cache.Get("1", fetch)
	if err != nil || second != "book" || secondStatus != StatusHit || calls != 1 {
		t.Fatalf("second = %v/%s, calls=%d, err=%v", second, secondStatus, calls, err)
	}
}

func TestCacheSingleFlightCoalescesConcurrentMisses(t *testing.T) {
	cache, err := New(true, time.Second, 10)
	if err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	started := make(chan struct{})
	release := make(chan struct{})
	fetch := func() (any, error) {
		if calls.Add(1) == 1 {
			close(started)
		}
		<-release
		return "shared", nil
	}

	const workers = 32
	var wg sync.WaitGroup
	wg.Add(workers)
	errs := make(chan error, workers)
	for i := 0; i < workers; i++ {
		go func() {
			defer wg.Done()
			value, _, err := cache.Get("hot", fetch)
			if err != nil {
				errs <- err
				return
			}
			if value != "shared" {
				errs <- &unexpectedValueError{value: value}
			}
		}()
	}
	<-started
	time.Sleep(20 * time.Millisecond)
	close(release)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatalf("fetch calls = %d; want 1", calls.Load())
	}
}

type unexpectedValueError struct{ value any }

func (e *unexpectedValueError) Error() string { return "unexpected shared value" }

func TestCacheExpiresAndDeleteInvalidates(t *testing.T) {
	cache, err := New(true, 20*time.Millisecond, 10)
	if err != nil {
		t.Fatal(err)
	}
	var calls int
	fetch := func() (any, error) {
		calls++
		return calls, nil
	}
	if _, _, err := cache.Get("1", fetch); err != nil {
		t.Fatal(err)
	}
	cache.Del("1")
	if value, status, err := cache.Get("1", fetch); err != nil || value != 2 || status != StatusMiss {
		t.Fatalf("after delete value=%v status=%s err=%v", value, status, err)
	}
	time.Sleep(30 * time.Millisecond)
	if value, status, err := cache.Get("1", fetch); err != nil || value != 3 || status != StatusMiss {
		t.Fatalf("after expiry value=%v status=%s err=%v", value, status, err)
	}
}

func TestDisabledCacheAlwaysFetches(t *testing.T) {
	cache, err := New(false, time.Second, 10)
	if err != nil {
		t.Fatal(err)
	}
	var calls int
	fetch := func() (any, error) { calls++; return calls, nil }
	_, status1, _ := cache.Get("1", fetch)
	_, status2, _ := cache.Get("1", fetch)
	if calls != 2 || status1 != StatusDisabled || status2 != StatusDisabled {
		t.Fatalf("calls=%d statuses=%s/%s", calls, status1, status2)
	}
}

func TestInvalidationDuringFetchDoesNotReinsertStaleValue(t *testing.T) {
	cache, err := New(true, time.Second, 10)
	if err != nil {
		t.Fatal(err)
	}

	started := make(chan struct{})
	release := make(chan struct{})
	firstDone := make(chan struct{})
	var calls atomic.Int32
	go func() {
		defer close(firstDone)
		_, _, _ = cache.Get("1", func() (any, error) {
			calls.Add(1)
			close(started)
			<-release
			return "old", nil
		})
	}()

	<-started
	cache.Del("1")
	close(release)
	<-firstDone

	value, status, err := cache.Get("1", func() (any, error) {
		calls.Add(1)
		return "new", nil
	})
	if err != nil || value != "new" || status != StatusMiss {
		t.Fatalf("value=%v status=%s err=%v", value, status, err)
	}
	if calls.Load() != 2 {
		t.Fatalf("fetch calls=%d; want 2 because stale in-flight value must not be cached", calls.Load())
	}
}
