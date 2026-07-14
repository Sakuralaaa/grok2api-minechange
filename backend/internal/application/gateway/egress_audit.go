package gateway

import (
	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/domain/audit"
	egressdomain "github.com/chenyme/grok2api/backend/internal/domain/egress"
	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
)

func applyAuditEgress(record *audit.Record, trace *infraegress.Trace, provider accountdomain.Provider) {
	if record == nil || trace == nil {
		return
	}
	selection, ok := trace.Selection(primaryEgressScope(provider))
	if !ok {
		return
	}
	record.EgressNodeName = selection.NodeName
	record.EgressScope = string(selection.Scope)
	if selection.Proxied {
		record.EgressMode = audit.EgressModeProxy
	} else {
		record.EgressMode = audit.EgressModeDirect
	}
	if selection.NodeID > 0 {
		id := selection.NodeID
		record.EgressNodeID = &id
	}
}

func primaryEgressScope(provider accountdomain.Provider) egressdomain.Scope {
	switch provider {
	case accountdomain.ProviderBuild:
		return egressdomain.ScopeBuild
	case accountdomain.ProviderConsole:
		return egressdomain.ScopeConsole
	default:
		return egressdomain.ScopeWeb
	}
}
