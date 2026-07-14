package console

import (
	"testing"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestAliasBindingsPointToListedCanonicalRoutes(t *testing.T) {
	listed := map[string]ModelSpec{}
	for _, spec := range Catalog() {
		if spec.Listed {
			listed[spec.PublicID] = spec
		}
	}
	for _, binding := range AliasBindings() {
		if binding.Provider != account.ProviderConsole {
			t.Fatalf("unexpected provider %s", binding.Provider)
		}
		if _, ok := listed[binding.CanonicalPublicID]; !ok {
			t.Fatalf("alias %s points to non-listed canonical %s", binding.Alias, binding.CanonicalPublicID)
		}
		if _, ok := listed[binding.Alias]; ok {
			t.Fatalf("alias %s should not itself be listed", binding.Alias)
		}
		if _, ok := ResolvePublic(binding.Alias); !ok {
			t.Fatalf("alias %s missing from catalog", binding.Alias)
		}
	}
}

func TestResolvePublicKeepsAliasEffort(t *testing.T) {
	spec, ok := ResolvePublic("grok-4.20-multi-agent-low-console")
	if !ok {
		t.Fatal("missing multi-agent low alias")
	}
	if spec.Effort != "low" || spec.AliasOf != "grok-4.20-heavy-low-console" {
		t.Fatalf("alias spec = %#v", spec)
	}
	canonical, ok := ResolvePublic(spec.AliasOf)
	if !ok || canonical.Effort != "low" {
		t.Fatalf("canonical = %#v", canonical)
	}
}
