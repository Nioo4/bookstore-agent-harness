package migrationrunner

import (
	"runtime"
	"strings"
	"testing"
)

func TestMigrationPreflightFailsClosedForEveryDirtyVersion(t *testing.T) {
	for _, version := range []uint{4, 5, 6} {
		preflight, err := migrationPreflight(migrationState{Exists: true, Version: version, Dirty: true})
		if err == nil || preflight {
			t.Fatalf("dirty v%d did not fail closed", version)
		}
		if !strings.Contains(err.Error(), RecoveryDocument) || !strings.Contains(err.Error(), "automatic recovery is forbidden") {
			t.Fatalf("dirty v%d error lacks safe recovery guidance: %v", version, err)
		}
	}
}

func TestMigrationPreflightOnlyVerifiesCleanV5(t *testing.T) {
	tests := []struct {
		name  string
		state migrationState
		want  bool
	}{
		{name: "new database", state: migrationState{}, want: false},
		{name: "clean v4", state: migrationState{Exists: true, Version: 4}, want: false},
		{name: "clean v5", state: migrationState{Exists: true, Version: 5}, want: true},
		{name: "clean v6", state: migrationState{Exists: true, Version: 6}, want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := migrationPreflight(test.state)
			if err != nil || got != test.want {
				t.Fatalf("preflight = %v, %v; want %v, nil", got, err, test.want)
			}
		})
	}
}

func TestFileSourceURLDoesNotEmbedCredentials(t *testing.T) {
	got, err := fileSourceURL("../../migrations")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got, "file:") || strings.Contains(got, "@") {
		t.Fatalf("unexpected migration source URL %q", got)
	}
	if runtime.GOOS == "windows" && strings.HasPrefix(got, "file:///") {
		t.Fatalf("Windows file source URL must place the drive in the host component: %q", got)
	}
}
