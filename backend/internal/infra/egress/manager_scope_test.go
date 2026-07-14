package egress

import (
	"reflect"
	"testing"

	domain "github.com/chenyme/grok2api/backend/internal/domain/egress"
)

func TestFallbackScopesIncludeGlobal(t *testing.T) {
	tests := []struct {
		scope domain.Scope
		want  []domain.Scope
	}{
		{scope: domain.ScopeBuild, want: []domain.Scope{domain.ScopeBuild, domain.ScopeGlobal}},
		{scope: domain.ScopeWeb, want: []domain.Scope{domain.ScopeWeb, domain.ScopeGlobal}},
		{scope: domain.ScopeWebAsset, want: []domain.Scope{domain.ScopeWebAsset, domain.ScopeWeb, domain.ScopeGlobal}},
		{scope: domain.ScopeConsole, want: []domain.Scope{domain.ScopeConsole, domain.ScopeWeb, domain.ScopeGlobal}},
	}
	for _, test := range tests {
		if got := fallbackScopes(test.scope); !reflect.DeepEqual(got, test.want) {
			t.Fatalf("fallbackScopes(%q) = %#v, want %#v", test.scope, got, test.want)
		}
	}
}
