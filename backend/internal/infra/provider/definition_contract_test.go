package provider_test

import (
	"reflect"
	"testing"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	modeldomain "github.com/chenyme/grok2api/backend/internal/domain/model"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
	"github.com/chenyme/grok2api/backend/internal/infra/provider/cli"
	"github.com/chenyme/grok2api/backend/internal/infra/provider/console"
	"github.com/chenyme/grok2api/backend/internal/infra/provider/web"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
)

func TestProductionProviderDefinitionsMatchImplementedCapabilities(t *testing.T) {
	cipher, err := security.NewCipher("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
	if err != nil {
		t.Fatal(err)
	}
	registry := provider.NewRegistry(
		cli.NewAdapter(cli.Config{}, cipher),
		web.NewAdapter(web.Config{}, nil, cipher, nil, nil),
		console.NewAdapter(console.Config{}, nil, cipher),
	)
	if err := registry.Validate(); err != nil {
		t.Fatalf("production registry invalid: %v", err)
	}

	tests := []struct {
		provider     account.Provider
		catalog      provider.ModelCatalogKind
		quota        provider.QuotaKind
		capabilities []modeldomain.Capability
		credential   provider.CredentialSurface
		conversation provider.ConversationSurface
		media        provider.MediaSurface
		inference    provider.InferencePolicy
	}{
		{
			provider: account.ProviderBuild, catalog: provider.ModelCatalogRemote, quota: provider.QuotaBilling,
			capabilities: []modeldomain.Capability{modeldomain.CapabilityResponses},
			credential:   provider.CredentialSurface{AuthType: account.AuthTypeOAuth, Import: true, Refresh: true, DeviceOAuth: true},
			conversation: provider.ConversationSurface{Responses: true, ChatCompletions: true, Messages: true, Compact: true, StoredResponses: true},
			inference:    provider.InferencePolicy{Usage: provider.UsageUpstream},
		},
		{
			provider: account.ProviderWeb, catalog: provider.ModelCatalogStatic, quota: provider.QuotaRemoteWindow,
			capabilities: []modeldomain.Capability{modeldomain.CapabilityChat, modeldomain.CapabilityImage, modeldomain.CapabilityImageEdit, modeldomain.CapabilityVideo},
			credential:   provider.CredentialSurface{AuthType: account.AuthTypeSSO, Import: true},
			conversation: provider.ConversationSurface{Responses: true, ChatCompletions: true, Messages: true, StoredResponses: true},
			media:        provider.MediaSurface{ImageGeneration: true, ImageEdit: true, VideoGeneration: true},
			inference:    provider.InferencePolicy{Usage: provider.UsageEstimated, RetryForbiddenAsEgress: true},
		},
		{
			provider: account.ProviderConsole, catalog: provider.ModelCatalogStatic, quota: provider.QuotaLocalWindow,
			capabilities: []modeldomain.Capability{modeldomain.CapabilityResponses},
			credential:   provider.CredentialSurface{AuthType: account.AuthTypeSSO, Import: true},
			conversation: provider.ConversationSurface{Responses: true, ChatCompletions: true, Messages: true},
			// UsageEstimated matches current gateway audit behavior for Console.
			inference: provider.InferencePolicy{Usage: provider.UsageEstimated},
		},
	}
	for _, test := range tests {
		t.Run(string(test.provider), func(t *testing.T) {
			definition, ok := registry.Definition(test.provider)
			if !ok {
				t.Fatal("definition not registered")
			}
			if definition.ModelNamespace != test.provider.ModelNamespace() || definition.ModelCatalog != test.catalog || definition.Quota != test.quota {
				t.Fatalf("definition identity = %#v", definition)
			}
			if !reflect.DeepEqual(definition.ModelCapabilities, test.capabilities) || definition.Credential != test.credential || definition.Conversation != test.conversation || definition.Media != test.media || definition.Inference != test.inference {
				t.Fatalf("definition capabilities = %#v", definition)
			}
			for _, operation := range []string{"responses", "chat", "messages"} {
				if !registry.SupportsConversation(test.provider, operation) {
					t.Fatalf("%s does not expose declared %s compatibility", test.provider, operation)
				}
			}
		})
	}
	if !registry.SupportsResponseCompaction(account.ProviderBuild) || registry.SupportsResponseCompaction(account.ProviderWeb) || registry.SupportsResponseCompaction(account.ProviderConsole) {
		t.Fatal("response compaction capability boundary is inconsistent")
	}
	if !registry.SupportsStoredResponses(account.ProviderBuild) || !registry.SupportsStoredResponses(account.ProviderWeb) || registry.SupportsStoredResponses(account.ProviderConsole) {
		t.Fatal("stored response capability boundary is inconsistent")
	}
	if !registry.RetryForbiddenAsEgress(account.ProviderWeb) || registry.RetryForbiddenAsEgress(account.ProviderBuild) || registry.RetryForbiddenAsEgress(account.ProviderConsole) {
		t.Fatal("retry-forbidden-as-egress boundary is inconsistent")
	}
	definition, _ := registry.Definition(account.ProviderWeb)
	definition.ModelCapabilities[0] = modeldomain.CapabilityResponses
	stored, _ := registry.Definition(account.ProviderWeb)
	if stored.ModelCapabilities[0] != modeldomain.CapabilityChat {
		t.Fatal("registry definition was mutated through a returned slice")
	}
}

func TestProviderDefinitionRejectsInconsistentMediaCapability(t *testing.T) {
	definition := provider.Definition{
		Provider:          account.ProviderWeb,
		ModelNamespace:    account.ProviderWeb.ModelNamespace(),
		ModelCatalog:      provider.ModelCatalogStatic,
		ModelCapabilities: []modeldomain.Capability{modeldomain.CapabilityImage},
		Quota:             provider.QuotaRemoteWindow,
		Credential:        provider.CredentialSurface{AuthType: account.AuthTypeSSO},
		Inference:         provider.InferencePolicy{Usage: provider.UsageEstimated},
	}
	if err := definition.Validate(); err == nil {
		t.Fatal("inconsistent media declaration was accepted")
	}
}

func TestRegistryValidateRequiresDefinitions(t *testing.T) {
	registry := provider.NewRegistry()
	if err := registry.Validate(); err == nil {
		t.Fatal("empty registry should fail validation")
	}
}
