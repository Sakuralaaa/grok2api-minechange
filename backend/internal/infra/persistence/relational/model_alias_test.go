package relational

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/domain/clientkey"
	"github.com/chenyme/grok2api/backend/internal/domain/model"
)

func TestModelRouteAliasesResolveAndMigrateLegacyRoutes(t *testing.T) {
	ctx := context.Background()
	database, err := OpenSQLite(ctx, filepath.Join(t.TempDir(), "model-aliases.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	models := NewModelRepository(database)
	accounts := NewAccountRepository(database)
	keys := NewClientKeyRepository(database)

	consoleAccount, _, err := accounts.UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderConsole, AuthType: account.AuthTypeSSO, Name: "console", SourceKey: "console-alias",
		EncryptedAccessToken: "token", Enabled: true, AuthStatus: account.AuthStatusActive, Priority: 1, MaxConcurrent: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := models.UpsertRoutes(ctx, []model.Route{{
		PublicID: "grok-4.3-medium-console", Provider: account.ProviderConsole, UpstreamModel: "grok-4.3",
		Capability: model.CapabilityResponses, Origin: model.OriginCatalog, Enabled: true,
	}}); err != nil {
		t.Fatal(err)
	}
	if err := models.UpsertRoutes(ctx, []model.Route{{
		PublicID: "grok-4.3-console", Provider: account.ProviderConsole, UpstreamModel: "grok-4.3",
		Capability: model.CapabilityResponses, Origin: model.OriginManual, Enabled: true,
	}}); err != nil {
		t.Fatal(err)
	}
	if err := models.ReplaceAccountCapabilities(ctx, consoleAccount.ID, []string{"grok-4.3"}, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	legacy, err := models.GetByPublicID(ctx, "grok-4.3-console")
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := models.GetByPublicID(ctx, "grok-4.3-medium-console")
	if err != nil {
		t.Fatal(err)
	}
	key, err := keys.Create(ctx, clientkey.Key{
		Name: "alias-key", Prefix: "alias", SecretHash: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		EncryptedSecret: "enc", Enabled: true, RPMLimit: 60, MaxConcurrent: 2, AllowedModels: []uint64{legacy.ID},
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = key

	if err := models.EnsureRouteAliases(ctx, []model.AliasBinding{{
		Alias: "grok-4.3-console", CanonicalPublicID: "grok-4.3-medium-console", Provider: account.ProviderConsole,
	}}); err != nil {
		t.Fatal(err)
	}

	resolved, err := models.GetByPublicID(ctx, "grok-4.3-console")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ID != canonical.ID || resolved.PublicID != "grok-4.3-medium-console" {
		t.Fatalf("alias resolved to %#v, want canonical id=%d", resolved, canonical.ID)
	}
	var count int64
	if err := database.db.WithContext(ctx).Model(&modelRouteModel{}).Where("public_id = ?", "grok-4.3-console").Count(&count).Error; err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("legacy alias route still present, count=%d", count)
	}
	stored, err := keys.Get(ctx, key.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(stored.AllowedModels) != 1 || stored.AllowedModels[0] != canonical.ID {
		t.Fatalf("allowed models = %#v, want [%d]", stored.AllowedModels, canonical.ID)
	}
}

func TestModelRouteRenamePreservesAlias(t *testing.T) {
	ctx := context.Background()
	database, err := OpenSQLite(ctx, filepath.Join(t.TempDir(), "model-rename.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	models := NewModelRepository(database)
	accountValue, _, err := NewAccountRepository(database).UpsertByIdentity(ctx, account.Credential{
		Provider: account.ProviderConsole, AuthType: account.AuthTypeSSO, Name: "console", SourceKey: "rename",
		EncryptedAccessToken: "token", Enabled: true, AuthStatus: account.AuthStatusActive, Priority: 1, MaxConcurrent: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	created, err := models.Create(ctx, model.Route{
		PublicID: "old-name-console", Provider: account.ProviderConsole, UpstreamModel: "grok-4.3",
		Capability: model.CapabilityResponses, Enabled: true,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := models.ReplaceAccountCapabilities(ctx, accountValue.ID, []string{"grok-4.3"}, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	created.PublicID = "new-name-console"
	updated, err := models.Update(ctx, created, nil)
	if err != nil {
		t.Fatal(err)
	}
	if updated.PublicID != "new-name-console" || updated.ID != created.ID {
		t.Fatalf("updated = %#v", updated)
	}
	resolved, err := models.GetByPublicID(ctx, "old-name-console")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ID != created.ID || resolved.PublicID != "new-name-console" {
		t.Fatalf("old name resolved to %#v", resolved)
	}
}

func TestModelAliasConflictsWithCanonicalPublicID(t *testing.T) {
	ctx := context.Background()
	database, err := OpenSQLite(ctx, filepath.Join(t.TempDir(), "model-alias-conflict.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	models := NewModelRepository(database)
	if err := models.UpsertRoutes(ctx, []model.Route{
		{PublicID: "canonical-a-console", Provider: account.ProviderConsole, UpstreamModel: "a", Capability: model.CapabilityResponses, Origin: model.OriginCatalog, Enabled: true},
		{PublicID: "canonical-b-console", Provider: account.ProviderConsole, UpstreamModel: "b", Capability: model.CapabilityResponses, Origin: model.OriginCatalog, Enabled: true},
	}); err != nil {
		t.Fatal(err)
	}
	if err := models.EnsureRouteAliases(ctx, []model.AliasBinding{{
		Alias: "canonical-b-console", CanonicalPublicID: "canonical-a-console", Provider: account.ProviderConsole,
	}}); err == nil {
		t.Fatal("expected alias conflict with existing public id")
	}
}
