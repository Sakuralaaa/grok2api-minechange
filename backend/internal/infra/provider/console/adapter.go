package console

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	egressdomain "github.com/chenyme/grok2api/backend/internal/domain/egress"
	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
	"github.com/chenyme/grok2api/backend/internal/infra/provider/conversation"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
)

const (
	defaultResponsesURL = "https://console.x.ai/v1/responses"
	defaultCluster      = "https://us-east-1.api.x.ai"
	defaultUserAgent    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
	maxResponseBytes    = 64 << 20
)

// Config controls console.x.ai upstream identity.
type Config struct {
	ResponsesURL            string
	Cluster                 string
	TeamID                  string
	UserAgent               string
	EnableSearchTools       bool
	TimeoutSeconds          int
	StreamHeartbeatInterval float64 // seconds; 0 disables; negative/unset defaults to 15
}

// Adapter implements the console.x.ai Responses transport with SSO cookies.
type Adapter struct {
	cfgMu  sync.RWMutex
	cfg    Config
	egress *infraegress.Manager
	cipher *security.Cipher
}

func NewAdapter(cfg Config, egress *infraegress.Manager, cipher *security.Cipher) *Adapter {
	return &Adapter{cfg: normalizeConfig(cfg), egress: egress, cipher: cipher}
}

func (a *Adapter) Provider() account.Provider { return account.ProviderConsole }

func (a *Adapter) UpdateConfig(cfg Config) {
	a.cfgMu.Lock()
	a.cfg = normalizeConfig(cfg)
	a.cfgMu.Unlock()
}

func (a *Adapter) config() Config {
	a.cfgMu.RLock()
	defer a.cfgMu.RUnlock()
	return a.cfg
}

func normalizeConfig(cfg Config) Config {
	if strings.TrimSpace(cfg.ResponsesURL) == "" {
		cfg.ResponsesURL = defaultResponsesURL
	}
	if strings.TrimSpace(cfg.Cluster) == "" {
		cfg.Cluster = defaultCluster
	}
	if strings.TrimSpace(cfg.UserAgent) == "" {
		cfg.UserAgent = defaultUserAgent
	}
	if cfg.TimeoutSeconds <= 0 {
		cfg.TimeoutSeconds = 300
	}
	// Negative means unset -> default 15s; explicit 0 disables heartbeats.
	if cfg.StreamHeartbeatInterval < 0 {
		cfg.StreamHeartbeatInterval = 15
	}
	return cfg
}

func (a *Adapter) QuotaMode(string) string { return "console" }

func (a *Adapter) TierOrder(string) []account.WebTier {
	return []account.WebTier{account.WebTierBasic, account.WebTierSuper, account.WebTierHeavy, account.WebTierAuto}
}

func (a *Adapter) PricingModel(string) string { return "" }

func (a *Adapter) ListModels(context.Context, account.Credential) ([]string, error) {
	return UpstreamModels(), nil
}

func (a *Adapter) ForwardResponse(ctx context.Context, request provider.ResponseResourceRequest) (*provider.Response, error) {
	cfg := a.config()
	token, err := a.cipher.Decrypt(request.Credential.EncryptedAccessToken)
	if err != nil {
		return nil, err
	}
	token = sanitizeSSOToken(token)
	if token == "" {
		return nil, provider.ErrUnauthorized
	}

	body := request.Body
	var conversationOptions conversation.ResponseOptions
	if request.NormalizeBody {
		if request.Operation == conversation.OperationChat || request.Operation == conversation.OperationMessages {
			body, conversationOptions, err = conversation.ConvertRequestWithOptions(body, request.Model, request.Operation)
		} else {
			body, err = conversation.ConvertRequest(body, request.Model, conversation.OperationResponses)
		}
		if err != nil {
			return invalidLocalResponse(err), nil
		}
	}

	publicID := strings.TrimSpace(request.PublicModel)
	if publicID == "" {
		publicID = request.Model
	}
	body, err = a.enrichConsoleBody(body, publicID, request.Model, request.Streaming)
	if err != nil {
		return invalidLocalResponse(err), nil
	}

	lease, err := a.egress.Acquire(ctx, egressdomain.ScopeWeb, "console:"+request.Credential.SourceKey)
	if err != nil {
		return nil, err
	}
	defer lease.Release()

	reqCtx := ctx
	var cancel context.CancelFunc
	if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > time.Duration(cfg.TimeoutSeconds)*time.Second {
		reqCtx, cancel = context.WithTimeout(ctx, time.Duration(cfg.TimeoutSeconds)*time.Second)
		defer cancel()
	}

	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, cfg.ResponsesURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	a.applyHeaders(req, token, lease, request.Streaming)

	resp, err := lease.Do(req)
	if err != nil {
		a.egress.Feedback(context.WithoutCancel(ctx), lease.NodeID, 0, err)
		return nil, err
	}
	a.egress.Feedback(context.WithoutCancel(ctx), lease.NodeID, resp.StatusCode, nil)

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		heartbeat := time.Duration(0)
		if cfg.StreamHeartbeatInterval > 0 {
			heartbeat = time.Duration(cfg.StreamHeartbeatInterval * float64(time.Second))
		}
		if request.Streaming {
			// Rewrite PublicID + inject synthetic reasoning before protocol conversion.
			resp.Body = TransformStream(resp.Body, publicID)
			if request.Operation == conversation.OperationChat || request.Operation == conversation.OperationMessages {
				resp.Body = conversation.ConvertResponseStreamWithOptions(resp.Body, request.Operation, conversationOptions)
			}
			// Heartbeats after conversion so chat/messages clients also stay alive.
			resp.Body = HeartbeatStream(resp.Body, heartbeat)
			resp.Header.Del("Content-Length")
			resp.Header.Set("Content-Type", "text/event-stream")
		} else {
			data, readErr := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
			_ = resp.Body.Close()
			if readErr != nil {
				return nil, readErr
			}
			if len(data) > maxResponseBytes {
				return nil, fmt.Errorf("console response exceeds 64 MiB")
			}
			normalized, normErr := NormalizeResponseJSON(data, publicID)
			if normErr != nil {
				return nil, normErr
			}
			data = normalized
			if request.Operation == conversation.OperationChat || request.Operation == conversation.OperationMessages {
				converted, convertErr := conversation.ConvertResponseJSONWithOptions(data, request.Operation, conversationOptions)
				if convertErr != nil {
					return nil, convertErr
				}
				data = converted
				if request.Operation == conversation.OperationChat {
					data = EnsureChatReasoningAliases(data)
				}
			}
			resp.Body = io.NopCloser(bytes.NewReader(data))
			resp.Header.Set("Content-Length", strconv.Itoa(len(data)))
			resp.Header.Set("Content-Type", "application/json")
		}
	}

	return &provider.Response{StatusCode: resp.StatusCode, Status: resp.Status, Header: resp.Header.Clone(), Body: resp.Body}, nil
}


func (a *Adapter) enrichConsoleBody(body []byte, publicID, upstreamModel string, streaming bool) ([]byte, error) {
	cfg := a.config()
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("invalid console request body: %w", err)
	}
	spec, ok := ResolvePublic(publicID)
	if !ok {
		if resolved, found := ResolveUpstream(upstreamModel); found {
			spec = resolved
			ok = true
		}
	}
	modelName := upstreamModel
	if ok && spec.UpstreamModel != "" {
		modelName = spec.UpstreamModel
	}
	payload["model"] = modelName
	payload["store"] = false
	payload["stream"] = streaming

	if ok && spec.Effort != "" && spec.Effort != "expert" && spec.Effort != "experimental" {
		payload["reasoning"] = map[string]any{"effort": mapEffort(spec.Effort)}
	} else if raw, exists := payload["reasoning"]; !exists || raw == nil {
		// keep client reasoning when present
	}

	if cfg.EnableSearchTools {
		if _, exists := payload["tools"]; !exists {
			payload["tools"] = defaultConsoleTools()
		}
	}
	if modelName == "grok-4.20-multi-agent-0309" {
		if _, exists := payload["max_output_tokens"]; !exists {
			payload["max_output_tokens"] = 2_000_000
		}
	}
	return json.Marshal(payload)
}

func mapEffort(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "none":
		return "none"
	case "minimal", "low":
		return "low"
	case "medium":
		return "medium"
	case "high":
		return "high"
	case "xhigh":
		return "xhigh"
	default:
		return "medium"
	}
}

func defaultConsoleTools() []map[string]any {
	return []map[string]any{
		{"type": "web_search", "enable_image_understanding": true},
		{"type": "x_search", "enable_video_understanding": true},
	}
}

func (a *Adapter) applyHeaders(req *http.Request, token string, lease *infraegress.Lease, streaming bool) {
	cfg := a.config()
	userAgent := cfg.UserAgent
	if lease != nil && strings.TrimSpace(lease.UserAgent) != "" {
		userAgent = lease.UserAgent
	}
	cfCookies := ""
	if lease != nil {
		cfCookies = lease.CFCookies
	}
	req.Header.Set("Accept", "*/*")
	if streaming {
		req.Header.Set("Accept", "text/event-stream")
		req.Header.Set("Accept-Encoding", "identity")
	} else {
		req.Header.Set("Accept-Encoding", "gzip, deflate, br, zstd")
	}
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6")
	req.Header.Set("Authorization", "Bearer anonymous")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Cookie", infraegress.BuildSSOCookie(token, cfCookies))
	req.Header.Set("Origin", "https://console.x.ai")
	req.Header.Set("Priority", "u=1, i")
	req.Header.Set("Referer", consoleReferer(cfg.TeamID))
	req.Header.Set("Sec-Fetch-Dest", "empty")
	req.Header.Set("Sec-Fetch-Mode", "cors")
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("X-Cluster", cfg.Cluster)
	req.Header.Set("x-statsig-id", randomStatsigID())
	req.Header.Set("x-xai-request-id", newRequestUUID())
	for key, value := range clientHints(userAgent) {
		req.Header.Set(key, value)
	}
}

func consoleReferer(teamID string) string {
	teamID = strings.TrimSpace(teamID)
	if teamID != "" {
		return "https://console.x.ai/team/" + teamID + "/chat-playground"
	}
	return "https://console.x.ai/"
}

func randomStatsigID() string {
	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	randPart := base64.RawURLEncoding.EncodeToString(buf)
	if len(randPart) > 8 {
		randPart = randPart[:8]
	}
	msg := "x1:TypeError: Cannot read properties of null (reading 'children[" + randPart + "]')"
	return base64.StdEncoding.EncodeToString([]byte(msg))
}

func newRequestUUID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	encoded := fmt.Sprintf("%x", value)
	return encoded[:8] + "-" + encoded[8:12] + "-" + encoded[12:16] + "-" + encoded[16:20] + "-" + encoded[20:]
}

var (
	edgeVersion   = regexp.MustCompile(`Edg/(\d+)`)
	chromeVersion = regexp.MustCompile(`(?:Chrome|Chromium)/(\d+)`)
)

func clientHints(userAgent string) map[string]string {
	version := "120"
	brand := "Google Chrome"
	if m := edgeVersion.FindStringSubmatch(userAgent); len(m) == 2 {
		version = m[1]
		brand = "Microsoft Edge"
	} else if m := chromeVersion.FindStringSubmatch(userAgent); len(m) == 2 {
		version = m[1]
	}
	platform := "Windows"
	switch {
	case strings.Contains(userAgent, "Mac OS X"), strings.Contains(userAgent, "Macintosh"):
		platform = "macOS"
	case strings.Contains(userAgent, "Android"):
		platform = "Android"
	case strings.Contains(userAgent, "iPhone"), strings.Contains(userAgent, "iPad"):
		platform = "iOS"
	case strings.Contains(userAgent, "Linux"):
		platform = "Linux"
	}
	mobile := "?0"
	if strings.Contains(userAgent, "Mobile") || platform == "Android" || platform == "iOS" {
		mobile = "?1"
	}
	return map[string]string{
		"Sec-Ch-Ua":          fmt.Sprintf(`"Chromium";v="%s", "%s";v="%s", "Not/A)Brand";v="99"`, version, brand, version),
		"Sec-Ch-Ua-Mobile":   mobile,
		"Sec-Ch-Ua-Platform": `"` + platform + `"`,
	}
}

func invalidLocalResponse(err error) *provider.Response {
	message := err.Error()
	data, _ := json.Marshal(map[string]any{"error": map[string]any{"message": message, "type": "invalid_request_error", "code": "invalid_request"}})
	return &provider.Response{
		StatusCode: http.StatusBadRequest,
		Status:     "400 Bad Request",
		Header:     http.Header{"Content-Type": []string{"application/json"}, "Content-Length": []string{strconv.Itoa(len(data))}},
		Body:       io.NopCloser(bytes.NewReader(data)),
	}
}

func sanitizeSSOToken(value string) string {
	value = strings.TrimSpace(value)
	if strings.HasPrefix(strings.ToLower(value), "sso=") {
		value = strings.TrimSpace(value[len("sso="):])
	}
	if token, _, found := strings.Cut(value, ";"); found {
		value = token
	}
	return strings.TrimSpace(strings.NewReplacer("\r", "", "\n", "", "\x00", "").Replace(value))
}
