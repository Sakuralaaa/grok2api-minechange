package account

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
	"github.com/chenyme/grok2api/backend/internal/infra/provider/conversation"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

const (
	buildInspectionModel         = "grok-4.5"
	buildInspectionCallTimeout   = 25 * time.Second
	buildInspectionQuotaCooldown = 24 * time.Hour
	buildInspectionMaxWorkers    = 16
	buildInspectionMaxAccounts   = 10000
)

var ErrBuildInspectionRunning = errors.New("Grok Build 账号巡检正在运行")

type BuildInspectionStartInput struct {
	Workers         int
	IncludeDisabled bool
	OnlyDisabled    bool
}

type BuildInspectionResult struct {
	AccountID      string     `json:"accountId"`
	Name           string     `json:"name"`
	Email          string     `json:"email,omitempty"`
	Disabled       bool       `json:"disabled"`
	Classification string     `json:"classification"`
	Action         string     `json:"action"`
	Reason         string     `json:"reason"`
	HTTPStatus     int        `json:"httpStatus,omitempty"`
	Model          string     `json:"model"`
	ErrorCode      string     `json:"errorCode,omitempty"`
	ErrorMessage   string     `json:"errorMessage,omitempty"`
	DurationMS     int64      `json:"durationMs"`
	InspectedAt    time.Time  `json:"inspectedAt"`
	CooldownUntil  *time.Time `json:"cooldownUntil,omitempty"`
	Applied        bool       `json:"applied,omitempty"`
	ApplyError     string     `json:"applyError,omitempty"`
}

type BuildInspectionSnapshot struct {
	Running         bool                    `json:"running"`
	Stopped         bool                    `json:"stopped"`
	Done            int                     `json:"done"`
	Total           int                     `json:"total"`
	Workers         int                     `json:"workers"`
	IncludeDisabled bool                    `json:"includeDisabled"`
	OnlyDisabled    bool                    `json:"onlyDisabled"`
	BaseURL         string                  `json:"baseURL,omitempty"`
	ResolvedBaseURL string                  `json:"resolvedBaseURL,omitempty"`
	UsingAPI        bool                    `json:"usingAPI"`
	StartedAt       *time.Time              `json:"startedAt,omitempty"`
	FinishedAt      *time.Time              `json:"finishedAt,omitempty"`
	Results         []BuildInspectionResult `json:"results"`
	Summary         map[string]int          `json:"summary"`
}

type BuildInspectionApplyResult struct {
	Enabled    int `json:"enabled"`
	Disabled   int `json:"disabled"`
	Deleted    int `json:"deleted"`
	CooledDown int `json:"cooledDown"`
	Failed     int `json:"failed"`
}

type buildProbeResponse struct {
	Status int
	Body   []byte
	Err    error
}

type buildProbeClassification struct {
	Classification string
	Action         string
	Reason         string
	Code           string
	Message        string
}

func normalizeInspectionWorkers(value int) int {
	if value <= 0 {
		return 6
	}
	return min(value, buildInspectionMaxWorkers)
}

func (s *Service) StartBuildInspection(ctx context.Context, input BuildInspectionStartInput) error {
	s.inspectionMu.Lock()
	if s.inspection.Running || s.automaticInspectionRunning {
		s.inspectionMu.Unlock()
		return ErrBuildInspectionRunning
	}
	s.inspectionMu.Unlock()

	workers := normalizeInspectionWorkers(input.Workers)
	strategy := s.buildInspectionStrategy()
	values, total, err := s.accounts.List(ctx, repository.AccountListQuery{
		Page:   repository.PageQuery{Limit: buildInspectionMaxAccounts + 1},
		Filter: repository.AccountListFilter{Provider: string(accountdomain.ProviderBuild), Now: s.now()},
	})
	if err != nil {
		return err
	}
	if total > buildInspectionMaxAccounts {
		return fmt.Errorf("单次最多巡检 %d 个 Grok Build 账号", buildInspectionMaxAccounts)
	}
	targets := make([]accountdomain.Credential, 0, len(values))
	for _, value := range values {
		if value.Provider != accountdomain.ProviderBuild {
			continue
		}
		disabled := !value.Enabled
		if input.OnlyDisabled && !disabled {
			continue
		}
		if !input.IncludeDisabled && !input.OnlyDisabled && disabled {
			continue
		}
		targets = append(targets, value)
	}

	s.inspectionMu.Lock()
	if s.inspection.Running || s.automaticInspectionRunning {
		s.inspectionMu.Unlock()
		return ErrBuildInspectionRunning
	}
	runCtx, cancel := context.WithCancel(context.Background())
	s.inspectionRunID++
	runID := s.inspectionRunID
	startedAt := s.now()
	s.inspectionCancel = cancel
	s.inspection = BuildInspectionSnapshot{
		Running: true, Done: 0, Total: len(targets), Workers: workers,
		IncludeDisabled: input.IncludeDisabled, OnlyDisabled: input.OnlyDisabled,
		BaseURL: strategy.BaseURL, ResolvedBaseURL: strategy.ResolvedBaseURL, UsingAPI: strategy.UsingAPI,
		StartedAt: &startedAt, Results: []BuildInspectionResult{}, Summary: map[string]int{},
	}
	s.inspectionMu.Unlock()

	go s.runBuildInspection(runCtx, runID, targets, workers)
	return nil
}

func (s *Service) StopBuildInspection() {
	s.inspectionMu.Lock()
	if s.inspection.Running {
		s.inspection.Stopped = true
		if s.inspectionCancel != nil {
			s.inspectionCancel()
		}
	}
	s.inspectionMu.Unlock()
}

func (s *Service) BuildInspectionSnapshot() BuildInspectionSnapshot {
	s.inspectionMu.Lock()
	value := s.inspection
	value.Results = append([]BuildInspectionResult(nil), value.Results...)
	value.Summary = make(map[string]int, len(s.inspection.Summary))
	for key, count := range s.inspection.Summary {
		value.Summary[key] = count
	}
	if value.Results == nil {
		value.Results = []BuildInspectionResult{}
	}
	s.inspectionMu.Unlock()
	strategy := s.buildInspectionStrategy()
	value.BaseURL, value.ResolvedBaseURL, value.UsingAPI = strategy.BaseURL, strategy.ResolvedBaseURL, strategy.UsingAPI
	return value
}

func (s *Service) buildInspectionStrategy() provider.BuildRuntimeStrategy {
	if s.providers == nil {
		return provider.BuildRuntimeStrategy{}
	}
	adapter, ok := s.providers.Responses(accountdomain.ProviderBuild)
	if !ok {
		return provider.BuildRuntimeStrategy{}
	}
	strategy, _ := adapter.(provider.BuildRuntimeStrategyAdapter)
	if strategy == nil {
		return provider.BuildRuntimeStrategy{}
	}
	return strategy.BuildRuntimeStrategy()
}

func (s *Service) runBuildInspection(ctx context.Context, runID uint64, targets []accountdomain.Credential, workers int) {
	jobs := make(chan accountdomain.Credential)
	var wait sync.WaitGroup
	for range workers {
		wait.Add(1)
		go func() {
			defer wait.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case credential, ok := <-jobs:
					if !ok {
						return
					}
					s.appendBuildInspectionResult(runID, s.inspectBuildAccount(ctx, credential))
				}
			}
		}()
	}
	for _, value := range targets {
		select {
		case <-ctx.Done():
			close(jobs)
			wait.Wait()
			s.finishBuildInspection(runID, targets, true)
			return
		case jobs <- value:
		}
	}
	close(jobs)
	wait.Wait()
	s.finishBuildInspection(runID, targets, ctx.Err() != nil)
}

func (s *Service) appendBuildInspectionResult(runID uint64, result BuildInspectionResult) {
	s.inspectionMu.Lock()
	defer s.inspectionMu.Unlock()
	if s.inspectionRunID != runID || !s.inspection.Running {
		return
	}
	s.inspection.Results = append(s.inspection.Results, result)
	s.inspection.Done = len(s.inspection.Results)
	s.inspection.Summary[result.Classification]++
}

func (s *Service) finishBuildInspection(runID uint64, targets []accountdomain.Credential, stopped bool) {
	s.inspectionMu.Lock()
	defer s.inspectionMu.Unlock()
	if s.inspectionRunID != runID {
		return
	}
	seen := make(map[string]struct{}, len(s.inspection.Results))
	for _, result := range s.inspection.Results {
		seen[result.AccountID] = struct{}{}
	}
	if stopped {
		for _, value := range targets {
			id := strconv.FormatUint(value.ID, 10)
			if _, ok := seen[id]; ok {
				continue
			}
			s.inspection.Results = append(s.inspection.Results, BuildInspectionResult{
				AccountID: id, Name: value.Name, Email: value.Email, Disabled: !value.Enabled,
				Classification: "cancelled", Action: "keep", Reason: "已停止，未探测",
				Model: buildInspectionModel, InspectedAt: s.now(),
			})
			s.inspection.Summary["cancelled"]++
		}
	}
	finishedAt := s.now()
	s.inspection.Running = false
	s.inspection.Stopped = stopped
	s.inspection.Done = len(s.inspection.Results)
	s.inspection.FinishedAt = &finishedAt
	s.inspectionCancel = nil
}

func (s *Service) inspectBuildAccount(ctx context.Context, credential accountdomain.Credential) BuildInspectionResult {
	started := time.Now()
	result := BuildInspectionResult{
		AccountID: strconv.FormatUint(credential.ID, 10), Name: credential.Name, Email: credential.Email,
		Disabled: !credential.Enabled, Model: buildInspectionModel, InspectedAt: s.now(),
	}
	if credential.AuthStatus == accountdomain.AuthStatusReauthRequired {
		result.Classification, result.Action, result.Reason = "reauth", "delete", "账号已标记为需要重新登录"
		result.DurationMS = time.Since(started).Milliseconds()
		return result
	}
	resolved, err := s.EnsureCredential(ctx, credential, false)
	if err != nil {
		result.Classification, result.Action, result.Reason = "probe_error", "keep", "凭据准备失败"
		if errors.Is(err, ErrCredentialRefreshPermanent) || errors.Is(err, provider.ErrUnauthorized) {
			result.Classification, result.Action, result.Reason = "reauth", "delete", "认证已过期或失效"
		}
		result.ErrorMessage = truncateInspectionText(err.Error(), 400)
		result.DurationMS = time.Since(started).Milliseconds()
		return result
	}
	adapter, ok := s.providers.Responses(accountdomain.ProviderBuild)
	if !ok {
		result.Classification, result.Action, result.Reason = "probe_error", "keep", "Grok Build Provider 未注册"
		result.DurationMS = time.Since(started).Milliseconds()
		return result
	}
	primaryBody := []byte(`{"model":"grok-4.5","input":"ping","stream":false}`)
	primary := s.callBuildInspection(ctx, adapter, resolved, "/responses", conversation.OperationResponses, primaryBody)
	if primary.Status == http.StatusTooManyRequests {
		classified := classifyBuildInspection(primary, result.Disabled)
		if classified.Classification == "probe_error" || classified.Classification == "rate_limited" {
			select {
			case <-ctx.Done():
			case <-time.After(350 * time.Millisecond):
				primary = s.callBuildInspection(ctx, adapter, resolved, "/responses", conversation.OperationResponses, primaryBody)
			}
		}
	}
	classified := classifyBuildInspection(primary, result.Disabled)
	if shouldFallbackBuildInspection(primary.Status, classified.Classification) {
		fallbackBody := []byte(`{"model":"grok-4.5","messages":[{"role":"user","content":"ping"}],"stream":false}`)
		fallback := s.callBuildInspection(ctx, adapter, resolved, "/chat/completions", conversation.OperationChat, fallbackBody)
		fallbackClassified := classifyBuildInspection(fallback, result.Disabled)
		if fallback.Err == nil {
			if classified.Classification == "reauth" || classified.Classification == "quota_exhausted" || classified.Classification == "permission_denied" {
				if fallbackClassified.Classification == "healthy" {
					classified.Reason += "；备用接口结果不一致，按主探测结果判定"
				}
			} else {
				primary, classified = fallback, fallbackClassified
			}
		}
	}
	result.HTTPStatus = primary.Status
	result.Classification, result.Action, result.Reason = classified.Classification, classified.Action, classified.Reason
	if classified.Classification != "healthy" {
		result.ErrorCode, result.ErrorMessage = classified.Code, truncateInspectionText(classified.Message, 400)
	}
	result.DurationMS = time.Since(started).Milliseconds()
	return result
}

func (s *Service) callBuildInspection(ctx context.Context, adapter provider.ResponseAdapter, credential accountdomain.Credential, path, operation string, body []byte) buildProbeResponse {
	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		callCtx, cancel := context.WithTimeout(ctx, buildInspectionCallTimeout)
		response, err := adapter.ForwardResponse(callCtx, provider.ResponseResourceRequest{
			Credential: credential, Method: http.MethodPost, Path: path, Model: buildInspectionModel,
			PublicModel: buildInspectionModel, Body: body, Operation: operation,
		})
		if err != nil {
			cancel()
			lastErr = err
			if !errors.Is(err, context.DeadlineExceeded) && !strings.Contains(strings.ToLower(err.Error()), "timeout") {
				return buildProbeResponse{Err: err}
			}
			continue
		}
		if response == nil {
			cancel()
			return buildProbeResponse{Err: errors.New("上游未返回响应")}
		}
		data, _, readErr := provider.ReadDiagnosticBody(response.Body)
		if response.Body != nil {
			_ = response.Body.Close()
		}
		cancel()
		if readErr != nil && !errors.Is(readErr, io.EOF) {
			return buildProbeResponse{Status: response.StatusCode, Err: readErr}
		}
		return buildProbeResponse{Status: response.StatusCode, Body: data}
	}
	return buildProbeResponse{Err: lastErr}
}

func classifyBuildInspection(response buildProbeResponse, disabled bool) buildProbeClassification {
	code, message := extractBuildInspectionError(response.Body)
	blob := strings.ToLower(strings.TrimSpace(code + " " + message))
	actionForDisable := "disable"
	if disabled {
		actionForDisable = "keep"
	}
	if response.Status == http.StatusUnauthorized || response.Status == http.StatusForbidden || inspectionContains(blob, "token is expired", "token has been invalidated", "invalid_grant", "unauthorized") {
		return buildProbeClassification{"reauth", "delete", "认证已过期或失效", code, message}
	}
	if response.Status == http.StatusPaymentRequired || inspectionContains(blob, "free-usage-exhausted", "used all the included free usage", "included free usage has been exhausted") {
		return buildProbeClassification{"quota_exhausted", "cooldown", "额度受限，建议进入 24 小时冷却池", code, message}
	}
	if response.Status == http.StatusTooManyRequests || strings.TrimSpace(code) == "429" || inspectionContains(blob, "too many requests", "rate limit", "rate_limit") {
		return buildProbeClassification{"rate_limited", "cooldown", "请求受到限流，建议进入 24 小时冷却池", code, message}
	}
	if inspectionContains(blob, "permission-denied", "chat endpoint is denied", "deactivated", "suspended", "banned") {
		return buildProbeClassification{"permission_denied", actionForDisable, fmt.Sprintf("对话权限被拒绝 (HTTP %d)", response.Status), code, message}
	}
	if response.Status == http.StatusNotFound || inspectionContains(blob, "not-found", "does not exist", "no access to it") {
		return buildProbeClassification{"model_unavailable", "keep", "测试模型不可用", code, message}
	}
	if response.Status >= 200 && response.Status < 300 {
		action := "keep"
		if disabled {
			action = "enable"
		}
		return buildProbeClassification{"healthy", action, "对话测试成功", "", ""}
	}
	if response.Err != nil {
		return buildProbeClassification{"probe_error", "keep", "探测请求失败", code, response.Err.Error()}
	}
	if response.Status > 0 {
		return buildProbeClassification{"probe_error", "keep", fmt.Sprintf("探测失败 (HTTP %d)", response.Status), code, message}
	}
	return buildProbeClassification{"unknown", "keep", "无法可靠分类", code, message}
}

func shouldFallbackBuildInspection(status int, classification string) bool {
	switch classification {
	case "reauth", "quota_exhausted", "rate_limited", "permission_denied", "healthy":
		return false
	}
	return status == 0 || status == http.StatusTooManyRequests || status >= 500 || classification == "probe_error" || classification == "unknown" || classification == "model_unavailable"
}

func extractBuildInspectionError(body []byte) (string, string) {
	body = bytes.TrimSpace(body)
	if len(body) == 0 {
		return "", ""
	}
	var data map[string]any
	if json.Unmarshal(body, &data) != nil {
		return "", truncateInspectionText(string(body), 400)
	}
	code := inspectionString(data["code"])
	message := ""
	switch value := data["error"].(type) {
	case map[string]any:
		if code == "" {
			code = inspectionString(value["code"])
		}
		message = firstInspectionValue(inspectionString(value["message"]), inspectionString(value["error"]))
	case string:
		message = value
	}
	if message == "" {
		message = inspectionString(data["message"])
	}
	return code, truncateInspectionText(message, 400)
}

func inspectionString(value any) string {
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case json.Number:
		return typed.String()
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	default:
		return ""
	}
}

func inspectionContains(value string, needles ...string) bool {
	value = strings.ToLower(value)
	for _, needle := range needles {
		if strings.Contains(value, strings.ToLower(needle)) {
			return true
		}
	}
	return false
}

func firstInspectionValue(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func truncateInspectionText(value string, limit int) string {
	value = strings.TrimSpace(value)
	runes := []rune(value)
	if limit <= 0 || len(runes) <= limit {
		return value
	}
	return string(runes[:limit]) + "…"
}

func (s *Service) ApplyBuildInspectionRecommendations(ctx context.Context, accountIDs []uint64) (BuildInspectionApplyResult, error) {
	selected := make(map[uint64]struct{}, len(accountIDs))
	for _, id := range accountIDs {
		if id > 0 {
			selected[id] = struct{}{}
		}
	}
	s.inspectionMu.Lock()
	if s.inspection.Running || s.automaticInspectionRunning {
		s.inspectionMu.Unlock()
		return BuildInspectionApplyResult{}, ErrBuildInspectionRunning
	}
	results := append([]BuildInspectionResult(nil), s.inspection.Results...)
	s.inspectionMu.Unlock()
	result := BuildInspectionApplyResult{}
	for _, item := range results {
		id, err := strconv.ParseUint(item.AccountID, 10, 64)
		if err != nil || id == 0 {
			continue
		}
		if len(selected) > 0 {
			if _, ok := selected[id]; !ok {
				continue
			}
		}
		var actionErr error
		var cooldownUntil *time.Time
		switch item.Action {
		case "enable":
			enabled := true
			_, actionErr = s.BatchUpdate(ctx, []uint64{id}, UpdateInput{Enabled: &enabled})
			if actionErr == nil {
				result.Enabled++
			}
		case "disable":
			enabled := false
			_, actionErr = s.BatchUpdate(ctx, []uint64{id}, UpdateInput{Enabled: &enabled})
			if actionErr == nil {
				result.Disabled++
			}
		case "delete":
			actionErr = s.Delete(ctx, id)
			if actionErr == nil {
				result.Deleted++
			}
		case "cooldown":
			cooldownUntil, actionErr = s.applyBuildInspectionCooldown(ctx, id, item.Disabled, buildInspectionQuotaCooldown)
			if actionErr == nil {
				result.CooledDown++
			}
		default:
			continue
		}
		if actionErr != nil {
			result.Failed++
		}
		s.markBuildInspectionApplied(item.AccountID, item.Action, cooldownUntil, actionErr)
	}
	return result, nil
}

func (s *Service) markBuildInspectionApplied(accountID, action string, cooldownUntil *time.Time, actionErr error) {
	s.inspectionMu.Lock()
	defer s.inspectionMu.Unlock()
	for index := range s.inspection.Results {
		item := &s.inspection.Results[index]
		if item.AccountID != accountID {
			continue
		}
		item.Applied = actionErr == nil
		if actionErr != nil {
			item.ApplyError = truncateInspectionText(actionErr.Error(), 300)
			return
		}
		item.ApplyError = ""
		item.Action = "keep"
		if action == "enable" {
			item.Disabled = false
		} else if action == "disable" {
			item.Disabled = true
		} else if action == "cooldown" {
			item.Disabled = false
			item.CooldownUntil = cooldownUntil
		}
		return
	}
}
