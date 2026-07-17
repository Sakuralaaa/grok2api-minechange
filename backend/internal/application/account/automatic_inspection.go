package account

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

// BuildInspectionConfig controls the Grok Build-only automatic credential inspection loop.
type BuildInspectionConfig struct {
	Enabled         bool
	Interval        time.Duration
	Workers         int
	IncludeDisabled bool
	QuotaAction     string
	ForbiddenAction string
	QuotaCooldown   time.Duration
}

type automaticBuildInspectionOutcome struct {
	classification string
	action         string
	err            error
}

func normalizeBuildInspectionConfig(value BuildInspectionConfig) BuildInspectionConfig {
	if value.Interval <= 0 {
		value.Interval = 6 * time.Hour
	}
	value.Workers = normalizeInspectionWorkers(value.Workers)
	if value.QuotaCooldown <= 0 {
		value.QuotaCooldown = buildInspectionQuotaCooldown
	}
	if value.QuotaAction == "" {
		value.QuotaAction = "cooldown"
	}
	if value.ForbiddenAction == "" {
		value.ForbiddenAction = "refresh_then_delete"
	}
	return value
}

// UpdateBuildInspectionConfig hot-reloads the automatic inspection schedule and actions.
func (s *Service) UpdateBuildInspectionConfig(value BuildInspectionConfig) {
	s.buildInspectionConfigMu.Lock()
	s.buildInspectionConfig = normalizeBuildInspectionConfig(value)
	s.buildInspectionConfigMu.Unlock()
	select {
	case s.buildInspectionWake <- struct{}{}:
	default:
	}
}

func (s *Service) currentBuildInspectionConfig() BuildInspectionConfig {
	s.buildInspectionConfigMu.RLock()
	value := s.buildInspectionConfig
	s.buildInspectionConfigMu.RUnlock()
	return normalizeBuildInspectionConfig(value)
}

// RunAutomaticBuildInspection waits for the configured interval and inspects only Grok Build credentials.
func (s *Service) RunAutomaticBuildInspection(ctx context.Context) error {
	for {
		cfg := s.currentBuildInspectionConfig()
		if !cfg.Enabled {
			select {
			case <-ctx.Done():
				return nil
			case <-s.buildInspectionWake:
				continue
			}
		}

		timer := time.NewTimer(cfg.Interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-s.buildInspectionWake:
			stopBuildInspectionTimer(timer)
			continue
		case <-timer.C:
		}

		if err := s.runAutomaticBuildInspection(ctx, cfg); err != nil && ctx.Err() == nil {
			s.logger.Warn("automatic_build_inspection_failed", "error", err)
		}
	}
}

func (s *Service) runAutomaticBuildInspection(ctx context.Context, cfg BuildInspectionConfig) error {
	s.inspectionMu.Lock()
	if s.inspection.Running || s.automaticInspectionRunning {
		s.inspectionMu.Unlock()
		s.logger.Info("automatic_build_inspection_skipped", "reason", "inspection already running")
		return nil
	}
	s.automaticInspectionRunning = true
	s.inspectionMu.Unlock()
	defer func() {
		s.inspectionMu.Lock()
		s.automaticInspectionRunning = false
		s.inspectionMu.Unlock()
	}()

	values, total, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: buildInspectionMaxAccounts + 1},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderBuild), Now: s.now()},
	})
	if err != nil {
		return err
	}
	if total > buildInspectionMaxAccounts {
		return errors.New("Grok Build 自动巡检账号数量超过单次上限")
	}
	targets := make([]accountdomain.Credential, 0, len(values))
	for _, value := range values {
		if value.Provider != accountdomain.ProviderBuild || (!cfg.IncludeDisabled && !value.Enabled) {
			continue
		}
		targets = append(targets, value)
	}

	startedAt := s.now()
	jobs := make(chan accountdomain.Credential)
	outcomes := make(chan automaticBuildInspectionOutcome)
	var workers sync.WaitGroup
	for worker := 0; worker < cfg.Workers; worker++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for credential := range jobs {
				outcomes <- s.inspectAndApplyBuildAccountAutomatically(ctx, credential, cfg)
			}
		}()
	}
	go func() {
		defer close(jobs)
		for _, credential := range targets {
			select {
			case <-ctx.Done():
				return
			case jobs <- credential:
			}
		}
	}()
	go func() {
		workers.Wait()
		close(outcomes)
	}()

	summary := make(map[string]int)
	actions := make(map[string]int)
	failures := 0
	for outcome := range outcomes {
		summary[outcome.classification]++
		if outcome.action != "" && outcome.action != "keep" {
			actions[outcome.action]++
		}
		if outcome.err != nil {
			failures++
			s.logger.Warn("automatic_build_inspection_account_failed", "error", outcome.err)
		}
	}
	s.logger.Info("automatic_build_inspection_completed",
		"started_at", startedAt, "finished_at", s.now(), "accounts", len(targets),
		"summary", summary, "actions", actions, "failures", failures,
	)
	return nil
}

func (s *Service) inspectAndApplyBuildAccountAutomatically(ctx context.Context, credential accountdomain.Credential, cfg BuildInspectionConfig) automaticBuildInspectionOutcome {
	result := s.inspectBuildAccount(ctx, credential)
	outcome := automaticBuildInspectionOutcome{classification: result.Classification, action: "keep"}
	if result.Classification == "quota_exhausted" || result.Classification == "rate_limited" {
		outcome.action, outcome.err = s.applyAutomaticBuildInspectionAction(ctx, credential, cfg.QuotaAction, cfg.QuotaCooldown)
		return outcome
	}
	if result.HTTPStatus != http.StatusForbidden && credential.AuthStatus != accountdomain.AuthStatusReauthRequired {
		return outcome
	}
	if cfg.ForbiddenAction != "refresh_then_delete" {
		outcome.action, outcome.err = s.applyAutomaticBuildInspectionAction(ctx, credential, cfg.ForbiddenAction, cfg.QuotaCooldown)
		return outcome
	}

	refreshed, err := s.ensureCredential(ctx, credential, true, true, false)
	if err != nil {
		outcome.err = err
		return outcome
	}
	rechecked := s.inspectBuildAccount(ctx, refreshed)
	outcome.classification = rechecked.Classification
	if rechecked.Classification == "quota_exhausted" || rechecked.Classification == "rate_limited" {
		outcome.action, outcome.err = s.applyAutomaticBuildInspectionAction(ctx, credential, cfg.QuotaAction, cfg.QuotaCooldown)
		return outcome
	}
	if rechecked.HTTPStatus != http.StatusForbidden {
		return outcome
	}
	outcome.err = s.Delete(ctx, credential.ID)
	if outcome.err == nil {
		outcome.action = "delete"
	}
	return outcome
}

func (s *Service) applyAutomaticBuildInspectionAction(ctx context.Context, credential accountdomain.Credential, action string, cooldown time.Duration) (string, error) {
	switch action {
	case "cooldown":
		_, err := s.applyBuildInspectionCooldown(ctx, credential.ID, !credential.Enabled, cooldown)
		return action, err
	case "disable":
		enabled := false
		_, err := s.BatchUpdate(ctx, []uint64{credential.ID}, UpdateInput{Enabled: &enabled})
		return action, err
	case "delete":
		return action, s.Delete(ctx, credential.ID)
	default:
		return "keep", nil
	}
}

func (s *Service) applyBuildInspectionCooldown(ctx context.Context, id uint64, wasDisabled bool, duration time.Duration) (*time.Time, error) {
	until := s.now().Add(duration)
	if err := s.accounts.UpdateHealth(ctx, id, 0, &until, "inspection quota cooldown", false); err != nil {
		return nil, err
	}
	if wasDisabled {
		enabled := true
		if _, err := s.BatchUpdate(ctx, []uint64{id}, UpdateInput{Enabled: &enabled, PreserveCooldown: true}); err != nil {
			return nil, err
		}
	}
	if s.sticky != nil {
		_ = s.sticky.DeleteByAccount(ctx, id)
	}
	return &until, nil
}

func stopBuildInspectionTimer(timer *time.Timer) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
}
