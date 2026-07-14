package relational

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	modeldomain "github.com/chenyme/grok2api/backend/internal/domain/model"
	"github.com/chenyme/grok2api/backend/internal/repository"
	"gorm.io/gorm"
)

func preserveModelRouteAlias(tx *gorm.DB, alias string, routeID uint64) error {
	alias = strings.TrimSpace(alias)
	if alias == "" || routeID == 0 {
		return nil
	}
	var route modelRouteModel
	if err := tx.Where("public_id = ? AND id <> ?", alias, routeID).First(&route).Error; err == nil {
		return fmt.Errorf("%w: 模型兼容名称 %q 与路由 %d 的规范名称冲突", repository.ErrConflict, alias, route.ID)
	} else if !errors.Is(err, gorm.ErrRecordNotFound) {
		return err
	}
	var existing modelRouteAliasModel
	err := tx.Where("alias = ?", alias).First(&existing).Error
	if err == nil {
		if existing.ModelRouteID == routeID {
			return nil
		}
		return fmt.Errorf("%w: 模型兼容名称 %q 已绑定路由 %d", repository.ErrConflict, alias, existing.ModelRouteID)
	}
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		return err
	}
	return tx.Create(&modelRouteAliasModel{Alias: alias, ModelRouteID: routeID}).Error
}

func ensureModelPublicIDNotAlias(tx *gorm.DB, publicID string, routeID uint64) error {
	publicID = strings.TrimSpace(publicID)
	if publicID == "" {
		return nil
	}
	var alias modelRouteAliasModel
	query := tx.Where("alias = ?", publicID)
	if routeID != 0 {
		query = query.Where("model_route_id <> ?", routeID)
	}
	err := query.First(&alias).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	return fmt.Errorf("%w: 模型公开 ID %q 已被路由 %d 保留为兼容名称", repository.ErrConflict, publicID, alias.ModelRouteID)
}

// EnsureRouteAliases 将兼容公开名绑定到规范路由，并迁移历史独立别名路由上的授权。
func (r *ModelRepository) EnsureRouteAliases(ctx context.Context, bindings []modeldomain.AliasBinding) error {
	if len(bindings) == 0 {
		return nil
	}
	return r.db.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, binding := range bindings {
			alias := strings.TrimSpace(binding.Alias)
			canonical := strings.TrimSpace(binding.CanonicalPublicID)
			if alias == "" || canonical == "" {
				return fmt.Errorf("模型别名绑定包含空值")
			}
			if alias == canonical {
				continue
			}
			if binding.Provider.IsValid() {
				if !modeldomain.ValidatePublicSuffix(alias, binding.Provider) || !modeldomain.ValidatePublicSuffix(canonical, binding.Provider) {
					return fmt.Errorf("模型别名 %q -> %q 必须使用合法渠道后缀", alias, canonical)
				}
			}
			var canonicalRoute modelRouteModel
			if err := tx.Where("public_id = ?", canonical).First(&canonicalRoute).Error; err != nil {
				if errors.Is(err, gorm.ErrRecordNotFound) {
					return fmt.Errorf("规范模型路由 %q 不存在，无法绑定别名 %q", canonical, alias)
				}
				return err
			}
			if binding.Provider.IsValid() && account.Provider(canonicalRoute.Provider) != binding.Provider {
				return fmt.Errorf("规范模型路由 %q 的 Provider 与别名绑定不一致", canonical)
			}

			var legacy modelRouteModel
			err := tx.Where("public_id = ? AND id <> ?", alias, canonicalRoute.ID).First(&legacy).Error
			if err == nil {
				// Only migrate historical independent alias routes. Never swallow catalog/canonical public IDs.
				if legacy.Origin != string(modeldomain.OriginManual) {
					return fmt.Errorf("%w: 模型兼容名称 %q 与路由 %d 的规范名称冲突", repository.ErrConflict, alias, legacy.ID)
				}
				if err := reassignClientKeyModelPermissions(tx, legacy.ID, canonicalRoute.ID); err != nil {
					return err
				}
				if err := tx.Where("model_route_id = ?", legacy.ID).Delete(&modelRouteAccountModel{}).Error; err != nil {
					return err
				}
				if err := tx.Where("model_route_id = ?", legacy.ID).Delete(&modelRouteAliasModel{}).Error; err != nil {
					return err
				}
				if err := tx.Delete(&modelRouteModel{}, legacy.ID).Error; err != nil {
					return err
				}
			} else if !errors.Is(err, gorm.ErrRecordNotFound) {
				return err
			}

			var existing modelRouteAliasModel
			err = tx.Where("alias = ?", alias).First(&existing).Error
			if err == nil {
				if existing.ModelRouteID == canonicalRoute.ID {
					continue
				}
				if err := tx.Model(&modelRouteAliasModel{}).Where("alias = ?", alias).Updates(map[string]any{
					"model_route_id": canonicalRoute.ID,
					"updated_at":     time.Now().UTC(),
				}).Error; err != nil {
					return mapError(err)
				}
				continue
			}
			if !errors.Is(err, gorm.ErrRecordNotFound) {
				return err
			}
			var conflict modelRouteModel
			if err := tx.Where("public_id = ?", alias).First(&conflict).Error; err == nil {
				return fmt.Errorf("%w: 模型兼容名称 %q 与路由 %d 的规范名称冲突", repository.ErrConflict, alias, conflict.ID)
			} else if !errors.Is(err, gorm.ErrRecordNotFound) {
				return err
			}
			if err := tx.Create(&modelRouteAliasModel{Alias: alias, ModelRouteID: canonicalRoute.ID}).Error; err != nil {
				return mapError(err)
			}
		}
		return nil
	})
}

func reassignClientKeyModelPermissions(tx *gorm.DB, fromRouteID, toRouteID uint64) error {
	if fromRouteID == 0 || toRouteID == 0 || fromRouteID == toRouteID {
		return nil
	}
	var permissions []clientKeyModelPermission
	if err := tx.Where("model_route_id = ?", fromRouteID).Find(&permissions).Error; err != nil {
		return err
	}
	for _, permission := range permissions {
		var count int64
		if err := tx.Model(&clientKeyModelPermission{}).
			Where("client_key_id = ? AND model_route_id = ?", permission.ClientKeyID, toRouteID).
			Count(&count).Error; err != nil {
			return err
		}
		if count == 0 {
			if err := tx.Create(&clientKeyModelPermission{ClientKeyID: permission.ClientKeyID, ModelRouteID: toRouteID}).Error; err != nil {
				return err
			}
		}
	}
	return tx.Where("model_route_id = ?", fromRouteID).Delete(&clientKeyModelPermission{}).Error
}
