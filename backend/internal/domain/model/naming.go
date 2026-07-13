package model

import (
	"strings"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

const (
	SuffixWeb     = "-web"
	SuffixBuild   = "-build"
	SuffixConsole = "-console"
)

// PublicSuffixForProvider returns the required public model suffix for a provider.
func PublicSuffixForProvider(provider account.Provider) string {
	switch provider {
	case account.ProviderWeb:
		return SuffixWeb
	case account.ProviderBuild:
		return SuffixBuild
	case account.ProviderConsole:
		return SuffixConsole
	default:
		return ""
	}
}

// EnsurePublicSuffix appends the provider suffix when missing.
func EnsurePublicSuffix(publicID string, provider account.Provider) string {
	publicID = strings.TrimSpace(publicID)
	suffix := PublicSuffixForProvider(provider)
	if publicID == "" || suffix == "" {
		return publicID
	}
	if strings.HasSuffix(publicID, suffix) {
		return publicID
	}
	return publicID + suffix
}

// ValidatePublicSuffix checks that a public model id matches its provider channel.
func ValidatePublicSuffix(publicID string, provider account.Provider) bool {
	publicID = strings.TrimSpace(publicID)
	suffix := PublicSuffixForProvider(provider)
	if publicID == "" || suffix == "" {
		return false
	}
	if !strings.HasSuffix(publicID, suffix) {
		return false
	}
	switch {
	case strings.HasSuffix(publicID, SuffixConsole):
		return provider == account.ProviderConsole
	case strings.HasSuffix(publicID, SuffixBuild):
		return provider == account.ProviderBuild
	case strings.HasSuffix(publicID, SuffixWeb):
		return provider == account.ProviderWeb
	default:
		return false
	}
}

// StripPublicSuffix removes a known channel suffix when present.
func StripPublicSuffix(publicID string) string {
	publicID = strings.TrimSpace(publicID)
	for _, suffix := range []string{SuffixConsole, SuffixBuild, SuffixWeb} {
		if strings.HasSuffix(publicID, suffix) {
			return strings.TrimSuffix(publicID, suffix)
		}
	}
	return publicID
}
