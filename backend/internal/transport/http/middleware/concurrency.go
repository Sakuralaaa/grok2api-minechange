package middleware

import (
	"net/http"
	"sync"

	"github.com/gin-gonic/gin"
)

// ConcurrencyGate provides a process-wide, immediately rejecting capacity
// guard for inference requests before they allocate upstream account leases.
type ConcurrencyGate struct {
	mu     sync.Mutex
	limit  int
	active int
}

func NewConcurrencyGate(limit int) *ConcurrencyGate {
	if limit < 1 {
		panic("middleware: 并发上限必须大于零")
	}
	return &ConcurrencyGate{limit: limit}
}

func (g *ConcurrencyGate) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		g.mu.Lock()
		if g.active >= g.limit {
			g.mu.Unlock()
			c.Header("Retry-After", "1")
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": gin.H{
				"code": "server_overloaded", "message": "服务并发已达到上限，请稍后重试", "param": nil, "type": "server_error",
			}})
			return
		}
		g.active++
		g.mu.Unlock()
		defer func() {
			g.mu.Lock()
			g.active--
			g.mu.Unlock()
		}()
		c.Next()
	}
}
