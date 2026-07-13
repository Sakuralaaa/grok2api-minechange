package console

import (
	"strings"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	modeldomain "github.com/chenyme/grok2api/backend/internal/domain/model"
)

// ModelSpec describes a console.x.ai public model route.
type ModelSpec struct {
	PublicID      string
	UpstreamModel string
	Effort        string // fixed effort when non-empty
	Listed        bool   // shown in GET /v1/models
	AliasOf       string // canonical public id when this is an alias
}

var catalog = []ModelSpec{
	// Recommended short names
	{PublicID: "grok-4.20-fast-console", UpstreamModel: "grok-4.20-0309-non-reasoning", Listed: true},
	{PublicID: "grok-4.20-expert-console", UpstreamModel: "grok-4.20-0309-reasoning", Effort: "expert", Listed: true},
	{PublicID: "grok-4.3-low-console", UpstreamModel: "grok-4.3", Effort: "low", Listed: true},
	{PublicID: "grok-4.3-medium-console", UpstreamModel: "grok-4.3", Effort: "medium", Listed: true},
	{PublicID: "grok-4.3-high-console", UpstreamModel: "grok-4.3", Effort: "high", Listed: true},
	{PublicID: "grok-4.20-heavy-low-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "low", Listed: true},
	{PublicID: "grok-4.20-heavy-medium-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "medium", Listed: true},
	{PublicID: "grok-4.20-heavy-high-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "high", Listed: true},
	{PublicID: "grok-4.20-heavy-xhigh-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "xhigh", Listed: true},
	{PublicID: "grok-4.5-console", UpstreamModel: "grok-4.5", Effort: "experimental", Listed: true},
	{PublicID: "grok-build-console", UpstreamModel: "grok-build-0.1", Listed: true},

	// Legacy aliases (callable, not listed)
	{PublicID: "grok-4.20-0309-non-reasoning-console", UpstreamModel: "grok-4.20-0309-non-reasoning", AliasOf: "grok-4.20-fast-console"},
	{PublicID: "grok-4.20-0309-console", UpstreamModel: "grok-4.20-0309", AliasOf: "grok-4.20-0309-console"},
	{PublicID: "grok-4.20-0309-reasoning-console", UpstreamModel: "grok-4.20-0309-reasoning", AliasOf: "grok-4.20-expert-console"},
	{PublicID: "grok-4.20-reasoning-console", UpstreamModel: "grok-4.20-0309-reasoning", AliasOf: "grok-4.20-expert-console"},
	{PublicID: "grok-4.3-console", UpstreamModel: "grok-4.3", AliasOf: "grok-4.3-medium-console"},
	{PublicID: "grok-4.3-beta-console", UpstreamModel: "grok-4.3", AliasOf: "grok-4.3-medium-console"},
	{PublicID: "grok-4.20-multi-agent-console", UpstreamModel: "grok-4.20-multi-agent-0309", AliasOf: "grok-4.20-heavy-medium-console"},
	{PublicID: "grok-4.20-heavy-console", UpstreamModel: "grok-4.20-multi-agent-0309", AliasOf: "grok-4.20-heavy-medium-console"},
	{PublicID: "grok-4.20-multi-agent-0309-console", UpstreamModel: "grok-4.20-multi-agent-0309", AliasOf: "grok-4.20-heavy-medium-console"},
	{PublicID: "grok-4.20-multi-agent-low-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "low", AliasOf: "grok-4.20-heavy-low-console"},
	{PublicID: "grok-4.20-multi-agent-medium-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "medium", AliasOf: "grok-4.20-heavy-medium-console"},
	{PublicID: "grok-4.20-multi-agent-high-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "high", AliasOf: "grok-4.20-heavy-high-console"},
	{PublicID: "grok-4.20-multi-agent-xhigh-console", UpstreamModel: "grok-4.20-multi-agent-0309", Effort: "xhigh", AliasOf: "grok-4.20-heavy-xhigh-console"},
}

var byPublic = map[string]ModelSpec{}

func init() {
	for _, spec := range catalog {
		byPublic[spec.PublicID] = spec
	}
}

// Catalog returns a copy of all console model specs.
func Catalog() []ModelSpec {
	return append([]ModelSpec(nil), catalog...)
}

// ListedRoutes returns DB seed routes for recommended console models only.
func ListedRoutes() []modeldomain.Route {
	values := make([]modeldomain.Route, 0, len(catalog))
	for _, spec := range catalog {
		if !spec.Listed {
			continue
		}
		values = append(values, modeldomain.Route{
			PublicID:      spec.PublicID,
			Provider:      account.ProviderConsole,
			UpstreamModel: spec.UpstreamModel,
			Capability:    modeldomain.CapabilityResponses,
			Origin:        modeldomain.OriginCatalog,
			Enabled:       true,
		})
	}
	return values
}

// AliasRoutes returns callable alias routes that resolve independently.
func AliasRoutes() []modeldomain.Route {
	values := make([]modeldomain.Route, 0)
	for _, spec := range catalog {
		if spec.Listed {
			continue
		}
		values = append(values, modeldomain.Route{
			PublicID:      spec.PublicID,
			Provider:      account.ProviderConsole,
			UpstreamModel: spec.UpstreamModel,
			Capability:    modeldomain.CapabilityResponses,
			Origin:        modeldomain.OriginManual,
			Enabled:       true,
		})
	}
	return values
}

// AllRoutes returns listed + alias routes for seeding.
func AllRoutes() []modeldomain.Route {
	return append(ListedRoutes(), AliasRoutes()...)
}

// ResolvePublic returns the console model spec for a public id.
func ResolvePublic(publicID string) (ModelSpec, bool) {
	spec, ok := byPublic[strings.TrimSpace(publicID)]
	return spec, ok
}

// ResolveUpstream returns the first listed spec for an upstream model.
func ResolveUpstream(upstreamModel string) (ModelSpec, bool) {
	for _, spec := range catalog {
		if spec.UpstreamModel == upstreamModel && spec.Listed {
			return spec, true
		}
	}
	for _, spec := range catalog {
		if spec.UpstreamModel == upstreamModel {
			return spec, true
		}
	}
	return ModelSpec{}, false
}

// UpstreamModels returns unique upstream model names.
func UpstreamModels() []string {
	seen := map[string]struct{}{}
	out := make([]string, 0)
	for _, spec := range catalog {
		if _, ok := seen[spec.UpstreamModel]; ok {
			continue
		}
		seen[spec.UpstreamModel] = struct{}{}
		out = append(out, spec.UpstreamModel)
	}
	return out
}

// IsListedPublic reports whether the public id should appear in /v1/models.
func IsListedPublic(publicID string) bool {
	spec, ok := ResolvePublic(publicID)
	return ok && spec.Listed
}
