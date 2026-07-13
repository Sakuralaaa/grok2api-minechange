package model

import (
	"testing"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestEnsureAndValidatePublicSuffix(t *testing.T) {
	if got := EnsurePublicSuffix("grok-4.5", account.ProviderBuild); got != "grok-4.5-build" {
		t.Fatalf("build suffix = %q", got)
	}
	if got := EnsurePublicSuffix("grok-chat-fast-web", account.ProviderWeb); got != "grok-chat-fast-web" {
		t.Fatalf("web keep = %q", got)
	}
	if !ValidatePublicSuffix("grok-4.3-high-console", account.ProviderConsole) {
		t.Fatal("console valid")
	}
	if ValidatePublicSuffix("grok-4.5-build", account.ProviderWeb) {
		t.Fatal("build must not validate as web")
	}
	if ValidatePublicSuffix("grok-4.5", account.ProviderBuild) {
		t.Fatal("unsuffixed must be invalid")
	}
}
