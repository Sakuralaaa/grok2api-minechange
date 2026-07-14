package egress

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	application "github.com/chenyme/grok2api/backend/internal/application/egress"
	domain "github.com/chenyme/grok2api/backend/internal/domain/egress"
	"github.com/chenyme/grok2api/backend/internal/infra/security"
	"github.com/chenyme/grok2api/backend/internal/repository"
	"golang.org/x/sync/singleflight"
)

const DefaultUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
const nodeSnapshotTTL = time.Second

type Lease struct {
	NodeID    uint64
	Scope     domain.Scope
	ProxyURL  string
	UserAgent string
	CFCookies string
	client    requestClient
	browser   *browserClient
	release   func()
}

type requestClient interface {
	Do(*http.Request) (*http.Response, error)
	CloseIdleConnections()
}

func (l *Lease) Do(request *http.Request) (*http.Response, error) {
	if l == nil || l.client == nil {
		return nil, fmt.Errorf("出口客户端未初始化")
	}
	return l.client.Do(request)
}
func (l *Lease) Release() {
	if l != nil && l.release != nil {
		l.release()
		l.release = nil
	}
}

type Manager struct {
	repository repository.EgressRepository
	cipher     *security.Cipher
	mu         sync.Mutex
	clients    map[uint64]cachedClient
	inflight   map[uint64]int
	nodes      map[domain.Scope]cachedNodeSnapshot
	nodeLoads  singleflight.Group
}

type cachedClient struct {
	fingerprint string
	client      requestClient
	browser     *browserClient
}

type cachedNodeSnapshot struct {
	values    []domain.Node
	expiresAt time.Time
}

func NewManager(repository repository.EgressRepository, cipher *security.Cipher) *Manager {
	return &Manager{repository: repository, cipher: cipher, clients: make(map[uint64]cachedClient), inflight: make(map[uint64]int), nodes: make(map[domain.Scope]cachedNodeSnapshot)}
}

func (m *Manager) Acquire(ctx context.Context, scope domain.Scope, affinity string) (*Lease, error) {
	lease, _, err := m.acquire(ctx, scope, affinity, true)
	return lease, err
}

func (m *Manager) AcquireIfConfigured(ctx context.Context, scope domain.Scope, affinity string) (*Lease, bool, error) {
	return m.acquire(ctx, scope, affinity, false)
}

func (m *Manager) acquire(ctx context.Context, scope domain.Scope, affinity string, allowDirect bool) (*Lease, bool, error) {
	now := time.Now().UTC()
	configured := false
	var available []domain.Node
	for _, candidateScope := range fallbackScopes(scope) {
		nodes, err := m.listNodes(ctx, candidateScope, now)
		if err != nil {
			return nil, false, err
		}
		configured = configured || len(nodes) > 0
		candidateAvailable := make([]domain.Node, 0, len(nodes))
		for _, node := range nodes {
			if node.Enabled && (node.CooldownUntil == nil || !now.Before(*node.CooldownUntil)) {
				candidateAvailable = append(candidateAvailable, node)
			}
		}
		if len(candidateAvailable) > 0 {
			available = candidateAvailable
			break
		}
	}
	if len(available) == 0 {
		if configured {
			return nil, false, fmt.Errorf("当前没有可用的 %s 出口节点", scope)
		}
		if !allowDirect {
			return nil, false, nil
		}
		available = []domain.Node{{ID: 0, Name: "direct", Scope: scope, Enabled: true, Health: 1}}
	}
	sort.SliceStable(available, func(i, j int) bool { return available[i].ID < available[j].ID })
	selected := m.selectNode(available, affinity)
	proxyURL, err := m.cipher.Decrypt(selected.EncryptedProxyURL)
	if err != nil {
		return nil, false, err
	}
	proxyURL, err = application.NormalizeProxyURL(proxyURL)
	if err != nil {
		return nil, false, err
	}
	cookies := ""
	if scope != domain.ScopeBuild {
		cookies, err = m.cipher.Decrypt(selected.EncryptedCloudflareCookie)
		if err != nil {
			return nil, false, err
		}
		cookies = application.SanitizeCloudflareCookies(cookies)
	}
	userAgent := ""
	if scope != domain.ScopeBuild {
		userAgent = strings.TrimSpace(selected.UserAgent)
	}
	if scope != domain.ScopeBuild && userAgent == "" {
		userAgent = DefaultUserAgent
	}
	cached, err := m.clientFor(selected.ID, scope, proxyURL, userAgent, cookies)
	if err != nil {
		return nil, false, err
	}
	m.mu.Lock()
	m.inflight[selected.ID]++
	m.mu.Unlock()
	var once sync.Once
	return &Lease{NodeID: selected.ID, Scope: scope, ProxyURL: proxyURL, UserAgent: userAgent, CFCookies: cookies, client: cached.client, browser: cached.browser, release: func() {
		once.Do(func() {
			m.mu.Lock()
			m.inflight[selected.ID]--
			if m.inflight[selected.ID] <= 0 {
				delete(m.inflight, selected.ID)
			}
			m.mu.Unlock()
		})
	}}, true, nil
}

func (m *Manager) listNodes(ctx context.Context, scope domain.Scope, now time.Time) ([]domain.Node, error) {
	m.mu.Lock()
	if snapshot, ok := m.nodes[scope]; ok && now.Before(snapshot.expiresAt) {
		values := append([]domain.Node(nil), snapshot.values...)
		m.mu.Unlock()
		return values, nil
	}
	m.mu.Unlock()
	loaded, err, _ := m.nodeLoads.Do(string(scope), func() (any, error) {
		checkTime := time.Now().UTC()
		m.mu.Lock()
		if snapshot, ok := m.nodes[scope]; ok && checkTime.Before(snapshot.expiresAt) {
			values := append([]domain.Node(nil), snapshot.values...)
			m.mu.Unlock()
			return values, nil
		}
		m.mu.Unlock()
		values, err := m.repository.ListEgressNodes(ctx, scope, repository.SortQuery{})
		if err != nil {
			return nil, err
		}
		m.mu.Lock()
		m.nodes[scope] = cachedNodeSnapshot{values: append([]domain.Node(nil), values...), expiresAt: checkTime.Add(nodeSnapshotTTL)}
		m.mu.Unlock()
		return values, nil
	})
	if err != nil {
		return nil, err
	}
	return append([]domain.Node(nil), loaded.([]domain.Node)...), nil
}

func (m *Manager) invalidateNodes(scope domain.Scope) {
	m.mu.Lock()
	delete(m.nodes, scope)
	m.mu.Unlock()
}

func fallbackScopes(scope domain.Scope) []domain.Scope {
	switch scope {
	case domain.ScopeWebAsset:
		return []domain.Scope{domain.ScopeWebAsset, domain.ScopeWeb, domain.ScopeGlobal}
	case domain.ScopeConsole:
		return []domain.Scope{domain.ScopeConsole, domain.ScopeWeb, domain.ScopeGlobal}
	case domain.ScopeGlobal:
		return []domain.Scope{domain.ScopeGlobal}
	default:
		return []domain.Scope{scope, domain.ScopeGlobal}
	}
}

func (m *Manager) TestNode(ctx context.Context, id uint64) (domain.ProbeResult, error) {
	value, err := m.repository.GetEgressNode(ctx, id)
	if err != nil {
		return domain.ProbeResult{}, err
	}
	result, err := m.probeNode(ctx, value)
	if err == nil && result.StatusCode != http.StatusForbidden {
		return result, nil
	}
	if strings.TrimSpace(value.FlareSolverrURL) == "" {
		return result, err
	}
	refreshed, refreshErr := m.RefreshClearance(ctx, id)
	if refreshErr != nil {
		if err != nil { return result, err }
		return result, refreshErr
	}
	value, getErr := m.repository.GetEgressNode(ctx, id)
	if getErr != nil { return refreshed, getErr }
	result, err = m.probeNode(ctx, value)
	result.ClearanceRefreshed = true
	return result, err
}

func (m *Manager) RefreshClearance(ctx context.Context, id uint64) (domain.ProbeResult, error) {
	value, err := m.repository.GetEgressNode(ctx, id)
	if err != nil { return domain.ProbeResult{}, err }
	fsURL := strings.TrimRight(strings.TrimSpace(value.FlareSolverrURL), "/")
	if fsURL == "" { return domain.ProbeResult{}, fmt.Errorf("FlareSolverr URL is not configured") }
	if parsed, parseErr := url.Parse(fsURL); parseErr != nil || parsed.Host == "" {
		return domain.ProbeResult{}, fmt.Errorf("invalid FlareSolverr URL")
	}
	proxyURL, err := m.cipher.Decrypt(value.EncryptedProxyURL)
	if err != nil { return domain.ProbeResult{}, err }
	proxyURL, err = application.NormalizeProxyURL(proxyURL)
	if err != nil { return domain.ProbeResult{}, err }
	payload := map[string]any{"cmd": "request.get", "url": "https://grok.com/", "maxTimeout": 90000}
	if proxyURL != "" { payload["proxy"] = map[string]any{"url": proxyURL} }
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fsURL+"/v1", bytes.NewReader(body))
	if err != nil { return domain.ProbeResult{}, err }
	req.Header.Set("Content-Type", "application/json")
	started := time.Now()
	client := &http.Client{Timeout: 100 * time.Second}
	resp, err := client.Do(req)
	if err != nil { return domain.ProbeResult{LatencyMS: time.Since(started).Milliseconds(), Message: "FlareSolverr connection failed"}, err }
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil { return domain.ProbeResult{}, err }
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return domain.ProbeResult{StatusCode: resp.StatusCode, LatencyMS: time.Since(started).Milliseconds(), Message: "FlareSolverr HTTP error"}, fmt.Errorf("flaresolverr status %d", resp.StatusCode)
	}
	var solved struct {
		Status string `json:"status"`
		Message string `json:"message"`
		Solution struct {
			UserAgent string `json:"userAgent"`
			Status int `json:"status"`
			Cookies []struct { Name string `json:"name"`; Value string `json:"value"` } `json:"cookies"`
		} `json:"solution"`
	}
	if err := json.Unmarshal(data, &solved); err != nil { return domain.ProbeResult{}, err }
	if solved.Status != "ok" { return domain.ProbeResult{}, fmt.Errorf("flaresolverr: %s", solved.Message) }
	parts := make([]string, 0, len(solved.Solution.Cookies))
	for _, cookie := range solved.Solution.Cookies {
		if strings.TrimSpace(cookie.Name) != "" && strings.TrimSpace(cookie.Value) != "" { parts = append(parts, cookie.Name+"="+cookie.Value) }
	}
	cookies := application.SanitizeCloudflareCookies(strings.Join(parts, "; "))
	if cookies == "" && (solved.Solution.Status < 200 || solved.Solution.Status >= 300) {
		return domain.ProbeResult{}, fmt.Errorf("flaresolverr returned no Cloudflare cookies")
	}
	value.EncryptedCloudflareCookie, err = m.cipher.Encrypt(cookies)
	if err != nil { return domain.ProbeResult{}, err }
	if ua := strings.TrimSpace(solved.Solution.UserAgent); ua != "" { value.UserAgent = ua }
	now := time.Now().UTC()
	value.LastClearanceAt = &now
	value.Health, value.FailureCount, value.CooldownUntil, value.LastError = 1, 0, nil, ""
	if _, err := m.repository.UpdateEgressNode(ctx, value); err != nil { return domain.ProbeResult{}, err }
	m.invalidateNodes(value.Scope)
	m.mu.Lock(); delete(m.clients, value.ID); m.mu.Unlock()
	message := "Cloudflare clearance refreshed"
	if cookies == "" {
		message = "No Cloudflare challenge detected; stale clearance cleared"
	}
	return domain.ProbeResult{ProxyConnected: true, StatusCode: solved.Solution.Status, LatencyMS: time.Since(started).Milliseconds(), ClearanceRefreshed: true, Message: message}, nil
}

func (m *Manager) probeNode(ctx context.Context, value domain.Node) (domain.ProbeResult, error) {
	proxyURL, err := m.cipher.Decrypt(value.EncryptedProxyURL)
	if err != nil { return domain.ProbeResult{}, err }
	proxyURL, err = application.NormalizeProxyURL(proxyURL)
	if err != nil { return domain.ProbeResult{}, err }
	cookies, err := m.cipher.Decrypt(value.EncryptedCloudflareCookie)
	if err != nil { return domain.ProbeResult{}, err }
	client, err := newBrowserClient(proxyURL)
	if err != nil { return domain.ProbeResult{}, err }
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://grok.com/", nil)
	if err != nil { return domain.ProbeResult{}, err }
	ua := strings.TrimSpace(value.UserAgent); if ua == "" { ua = DefaultUserAgent }
	req.Header.Set("User-Agent", ua)
	if sanitized := application.SanitizeCloudflareCookies(cookies); sanitized != "" { req.Header.Set("Cookie", sanitized) }
	started := time.Now()
	resp, err := client.Do(req)
	if err != nil { return domain.ProbeResult{LatencyMS: time.Since(started).Milliseconds(), Message: "proxy transport failed"}, err }
	defer resp.Body.Close()
	result := domain.ProbeResult{ProxyConnected: true, StatusCode: resp.StatusCode, LatencyMS: time.Since(started).Milliseconds()}
	switch { case resp.StatusCode >= 200 && resp.StatusCode < 400: result.Message = "proxy and Grok are reachable"; case resp.StatusCode == http.StatusForbidden: result.Message = "Cloudflare rejected the request"; default: result.Message = fmt.Sprintf("Grok returned HTTP %d", resp.StatusCode) }
	return result, nil
}

func (m *Manager) selectNode(nodes []domain.Node, affinity string) domain.Node {
	if affinity != "" {
		digest := sha256.Sum256([]byte(affinity))
		selected := nodes[int(binary.BigEndian.Uint64(digest[:8])%uint64(len(nodes)))]
		if selected.Health >= 0.8 || len(nodes) == 1 {
			return selected
		}
		for _, node := range nodes {
			if node.Health > selected.Health {
				selected = node
			}
		}
		return selected
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	best := nodes[0]
	for _, node := range nodes[1:] {
		if m.inflight[node.ID] < m.inflight[best.ID] || (m.inflight[node.ID] == m.inflight[best.ID] && node.Health > best.Health) {
			best = node
		}
	}
	return best
}

func (m *Manager) clientFor(id uint64, scope domain.Scope, proxyURL, userAgent, cookies string) (cachedClient, error) {
	clientKind := "browser"
	if scope == domain.ScopeBuild {
		clientKind = "build"
	}
	fingerprint := fmt.Sprintf("%x", sha256.Sum256([]byte(clientKind+"\x00"+proxyURL+"\x00"+userAgent+"\x00"+cookies)))
	m.mu.Lock()
	defer m.mu.Unlock()
	if cached, ok := m.clients[id]; ok && cached.fingerprint == fingerprint {
		return cached, nil
	}
	var value cachedClient
	value.fingerprint = fingerprint
	if scope == domain.ScopeBuild {
		client, err := newBuildClient(proxyURL)
		if err != nil {
			return cachedClient{}, err
		}
		value.client = client
	} else {
		client, err := newBrowserClient(proxyURL)
		if err != nil {
			return cachedClient{}, err
		}
		value.client = client
		value.browser = client
	}
	if previous, exists := m.clients[id]; exists && previous.client != nil {
		previous.client.CloseIdleConnections()
	}
	m.clients[id] = value
	return value, nil
}


func (m *Manager) Feedback(ctx context.Context, nodeID uint64, status int, transportErr error) {
	m.FeedbackForScope(ctx, domain.ScopeWeb, nodeID, status, transportErr)
}

func (m *Manager) FeedbackForScope(ctx context.Context, scope domain.Scope, nodeID uint64, status int, transportErr error) {
	if nodeID == 0 {
		if transportErr != nil || status >= 500 || (scope != domain.ScopeBuild && status == http.StatusForbidden) {
			m.mu.Lock()
			if cached, ok := m.clients[0]; ok && cached.client != nil {
				cached.client.CloseIdleConnections()
			}
			delete(m.clients, 0)
			m.mu.Unlock()
		}
		return
	}
	value, err := m.repository.GetEgressNode(ctx, nodeID)
	if err != nil {
		return
	}
	now := time.Now().UTC()
	switch {
	case transportErr == nil && status >= 200 && status < 400:
		value.Health = min(1, value.Health+0.1)
		value.FailureCount = 0
		value.CooldownUntil = nil
		value.LastError = ""
	case status == http.StatusUnauthorized || status == http.StatusTooManyRequests:
		return
	case scope == domain.ScopeBuild && status == http.StatusForbidden:
		// Build 403 可能是账号权限、额度、Token 或出口策略；不要误判为 Web anti-bot。
		return
	case status == http.StatusForbidden:
		value.FailureCount++
		value.Health = max(0.05, value.Health*0.7)
		value.CooldownUntil = nil
		value.LastError = "anti-bot rejection"
		m.mu.Lock()
		if cached, ok := m.clients[nodeID]; ok && cached.client != nil {
			cached.client.CloseIdleConnections()
		}
		delete(m.clients, nodeID)
		m.mu.Unlock()
	default:
		value.FailureCount++
		value.Health = max(0.05, value.Health*0.7)
		cooldown := min(10*time.Minute, 30*time.Second*time.Duration(1<<min(value.FailureCount-1, 4)))
		until := now.Add(cooldown)
		value.CooldownUntil = &until
		if transportErr != nil {
			value.LastError = "transport error"
		} else {
			value.LastError = fmt.Sprintf("upstream status %d", status)
		}
		m.mu.Lock()
		if cached, ok := m.clients[nodeID]; ok && cached.client != nil {
			cached.client.CloseIdleConnections()
		}
		delete(m.clients, nodeID)
		m.mu.Unlock()
	}
	if _, err := m.repository.UpdateEgressNode(ctx, value); err == nil {
		m.invalidateNodes(value.Scope)
	}
if status == http.StatusForbidden && strings.TrimSpace(value.FlareSolverrURL) != "" {
		go func(nodeID uint64) {
			refreshCtx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
			defer cancel()
			_, _ = m.RefreshClearance(refreshCtx, nodeID)
		}(nodeID)
	}
}

func BuildSSOCookie(token, cloudflareCookies string) string {
	token = strings.TrimSpace(token)
	if strings.HasPrefix(strings.ToLower(token), "sso=") {
		token = strings.TrimSpace(token[len("sso="):])
	}
	if value, _, found := strings.Cut(token, ";"); found {
		token = strings.TrimSpace(value)
	}
	token = strings.NewReplacer("\r", "", "\n", "", "\x00", "").Replace(token)
	cookies := "sso=" + token + "; sso-rw=" + token
	if sanitized := application.SanitizeCloudflareCookies(cloudflareCookies); sanitized != "" {
		cookies += "; " + sanitized
	}
	return cookies
}
