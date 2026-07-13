package web

import (
	"strings"
	"testing"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

func TestParseImportedCredentialsAcceptsOneSSOTokenPerLine(t *testing.T) {
	adapter := &Adapter{}
	values, err := adapter.ParseImportedCredentials([]byte("token-one\nsso=token-two; other=drop\n\ntoken-one\n"))
	if err != nil {
		t.Fatal(err)
	}
	// Web SSO import dual-seeds grok_web + grok_console for each unique token.
	if len(values) != 4 {
		t.Fatalf("credentials = %#v", values)
	}
	if values[0].AccessToken != "token-one" || values[0].Provider != account.ProviderWeb {
		t.Fatalf("first web token = %#v", values[0])
	}
	if values[1].AccessToken != "token-one" || values[1].Provider != account.ProviderConsole {
		t.Fatalf("first console token = %#v", values[1])
	}
	if values[2].AccessToken != "token-two" || values[2].Provider != account.ProviderWeb {
		t.Fatalf("second web token = %#v", values[2])
	}
	if values[3].AccessToken != "token-two" || values[3].Provider != account.ProviderConsole {
		t.Fatalf("second console token = %#v", values[3])
	}
	for _, value := range values {
		if value.AuthType != account.AuthTypeSSO {
			t.Fatalf("credential = %#v", value)
		}
		if value.Provider == account.ProviderWeb && value.WebTier != account.WebTierAuto {
			t.Fatalf("web credential tier = %#v", value)
		}
		if value.Provider == account.ProviderConsole && value.WebTier != account.WebTierBasic {
			t.Fatalf("console credential tier = %#v", value)
		}
	}
}

func TestParseImportedCredentialsRejectsOversizedPlainToken(t *testing.T) {
	adapter := &Adapter{}
	_, err := adapter.ParseImportedCredentials([]byte(strings.Repeat("x", maxSSOTokenBytes+1)))
	if err == nil {
		t.Fatal("expected oversized token error")
	}
}

func TestWebCredentialJSONUsesCurrentDocumentShape(t *testing.T) {
	adapter := &Adapter{}
	values, err := adapter.ParseImportedCredentials([]byte(`{"provider":"grok_web","accounts":[{"name":"primary","sso_token":"token-one","tier":"super"}]}`))
	if err != nil || len(values) != 2 {
		t.Fatalf("credentials = %#v, err = %v", values, err)
	}
	if values[0].Provider != account.ProviderWeb || values[0].WebTier != account.WebTierSuper {
		t.Fatalf("web credential = %#v", values[0])
	}
	if values[1].Provider != account.ProviderConsole || values[1].AccessToken != "token-one" {
		t.Fatalf("console dual-seed = %#v", values[1])
	}
	// Marshal only web accounts for export document shape checks.
	webOnly := values[:1]
	data, err := adapter.MarshalCredentials(webOnly)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), `"version"`) {
		t.Fatalf("export contains version metadata: %s", data)
	}
	if _, err := adapter.ParseImportedCredentials([]byte(`{"basic":["token-one"]}`)); err == nil {
		t.Fatal("legacy tier pools were accepted")
	}
}
