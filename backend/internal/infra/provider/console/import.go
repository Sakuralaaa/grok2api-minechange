package console

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
)

const (
	maxImportAccounts = 10000
	maxSSOTokenBytes  = 16 << 10
)

type importDocument struct {
	Provider string        `json:"provider"`
	Accounts []importEntry `json:"accounts"`
}

type importEntry struct {
	Name     string `json:"name"`
	SSOToken string `json:"sso_token"`
	Token    string `json:"token"`
}

func (a *Adapter) ParseImportedCredentials(data []byte) ([]provider.CredentialSeed, error) {
	trimmed := strings.TrimSpace(string(data))
	if trimmed == "" {
		return nil, fmt.Errorf("empty Grok Console credentials")
	}
	if !strings.HasPrefix(trimmed, "{") {
		return parsePlainTextCredentials(trimmed)
	}
	var document importDocument
	if err := json.Unmarshal(data, &document); err != nil {
		return nil, fmt.Errorf("seed Grok Console routes: %w", err)
	}
	if document.Provider != "" && document.Provider != string(account.ProviderConsole) && document.Provider != string(account.ProviderWeb) {
		return nil, fmt.Errorf("unexpected provider, expected %s", account.ProviderConsole)
	}
	entries := document.Accounts
	if len(entries) == 0 {
		return nil, fmt.Errorf("empty Grok Console credentials")
	}
	if len(entries) > maxImportAccounts {
		return nil, provider.ErrCredentialLimit
	}
	seen := map[string]struct{}{}
	result := make([]provider.CredentialSeed, 0, len(entries))
	for index, entry := range entries {
		token := sanitizeSSOToken(firstNonEmpty(entry.SSOToken, entry.Token))
		if token == "" {
			return nil, fmt.Errorf("entry %d missing sso_token", index+1)
		}
		if len(token) > maxSSOTokenBytes {
			return nil, fmt.Errorf("entry %d sso_token exceeds 16 KiB", index+1)
		}
		if _, exists := seen[token]; exists {
			continue
		}
		seen[token] = struct{}{}
		name := strings.TrimSpace(entry.Name)
		if name == "" {
			name = fmt.Sprintf("Grok Console %s", security.HashToken(token)[:8])
		}
		result = append(result, provider.CredentialSeed{
			Provider: account.ProviderConsole, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
			Name: name, SourceKey: "console-sso:" + security.HashToken(token), AccessToken: token,
		})
	}
	return result, nil
}

func parsePlainTextCredentials(value string) ([]provider.CredentialSeed, error) {
	lines := strings.Split(value, "\n")
	seen := map[string]struct{}{}
	result := make([]provider.CredentialSeed, 0, len(lines))
	for index, line := range lines {
		token := sanitizeSSOToken(line)
		if token == "" {
			continue
		}
		if len(token) > maxSSOTokenBytes {
			return nil, fmt.Errorf("line %d sso token exceeds 16 KiB", index+1)
		}
		if _, exists := seen[token]; exists {
			continue
		}
		seen[token] = struct{}{}
		result = append(result, provider.CredentialSeed{
			Provider: account.ProviderConsole, AuthType: account.AuthTypeSSO, WebTier: account.WebTierBasic,
			Name: "Grok Console " + security.HashToken(token)[:8], SourceKey: "console-sso:" + security.HashToken(token), AccessToken: token,
		})
		if len(result) > maxImportAccounts {
			return nil, provider.ErrCredentialLimit
		}
	}
	if len(result) == 0 {
		return nil, fmt.Errorf("???????? sso token")
	}
	return result, nil
}

func (a *Adapter) MarshalCredentials(values []provider.CredentialSeed) ([]byte, error) {
	document := importDocument{Provider: string(account.ProviderConsole), Accounts: make([]importEntry, 0, len(values))}
	for _, value := range values {
		document.Accounts = append(document.Accounts, importEntry{Name: value.Name, SSOToken: value.AccessToken})
	}
	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
