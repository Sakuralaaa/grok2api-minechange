package model

import (
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
)

type Capability string

type Origin string

const (
	CapabilityResponses Capability = "responses"
	CapabilityChat      Capability = "chat"
	CapabilityImage     Capability = "image"
	CapabilityImageEdit Capability = "image_edit"
	CapabilityVideo     Capability = "video"
)

const (
	OriginCatalog    Origin = "catalog"
	OriginDiscovered Origin = "discovered"
	OriginManual     Origin = "manual"
)

// AliasBinding 将兼容公开模型名绑定到规范公开模型名。
type AliasBinding struct {
	Alias             string
	CanonicalPublicID string
	Provider          account.Provider
}

// Route 表示公开模型名到上游模型名的稳定映射。
type Route struct {
	ID                uint64
	PublicID          string
	Provider          account.Provider
	UpstreamModel     string
	Capability        Capability
	Origin            Origin
	Enabled           bool
	BoundAccountIDs   []uint64
	Aliases           []string
	SupportedAccounts int
	SyncedAccounts    int
	TotalAccounts     int
	LastSyncedAt      *time.Time
	CreatedAt         time.Time
	UpdatedAt         time.Time
}
