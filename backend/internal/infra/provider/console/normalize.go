package console

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var resetDurationPattern = regexp.MustCompile(`(?i)(\d+)\s*([dhms])`)

func normalizeRequest(body []byte, spec ModelSpec) ([]byte, error) {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("parse Console Responses request: %w", err)
	}
	if value, exists := payload["store"]; exists {
		store, ok := value.(bool)
		if !ok {
			return nil, fmt.Errorf("Console store must be a boolean")
		}
		if store {
			return nil, fmt.Errorf("Grok Console does not support store: true; use stateless Responses input replay")
		}
	}
	if value, exists := payload["previous_response_id"]; exists && value != nil {
		previousID, ok := value.(string)
		if !ok {
			return nil, fmt.Errorf("Console previous_response_id must be a string")
		}
		if strings.TrimSpace(previousID) != "" {
			return nil, fmt.Errorf("Grok Console does not support previous_response_id; replay the full input")
		}
		delete(payload, "previous_response_id")
	}
	payload["model"] = spec.UpstreamModel
	payload["store"] = false
	delete(payload, "metadata")
	normalizeReasoning(payload, spec)
	return json.Marshal(payload)
}

func normalizeReasoning(payload map[string]any, spec ModelSpec) {
	effort := normalizeEffort(spec.Effort)
	if effort == "" {
		// Keep caller-provided reasoning only when this public model pins no effort.
		return
	}
	reasoning, _ := payload["reasoning"].(map[string]any)
	if reasoning == nil {
		reasoning = make(map[string]any)
	}
	reasoning["effort"] = effort
	payload["reasoning"] = reasoning
}

func normalizeEffort(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "minimal", "low":
		return "low"
	case "medium":
		return "medium"
	case "high", "xhigh", "expert":
		return "high"
	default:
		return ""
	}
}

func mergeSearchTools(payload map[string]any) error {
	defaults := []any{
		map[string]any{"type": "web_search", "enable_image_understanding": true},
		map[string]any{"type": "x_search", "enable_video_understanding": true},
	}
	positions := map[string]int{"web_search": 0, "x_search": 1}
	result := append([]any(nil), defaults...)
	if value, exists := payload["tools"]; exists && value != nil {
		tools, ok := value.([]any)
		if !ok {
			return fmt.Errorf("Console tools must be an array")
		}
		for _, tool := range tools {
			identity := toolIdentity(tool)
			if index, exists := positions[identity]; identity != "" && exists {
				result[index] = tool
				continue
			}
			if identity != "" {
				positions[identity] = len(result)
			}
			result = append(result, tool)
		}
	}
	payload["tools"] = result
	if _, exists := payload["tool_choice"]; !exists {
		payload["tool_choice"] = "auto"
	}
	return nil
}

func toolIdentity(value any) string {
	tool, ok := value.(map[string]any)
	if !ok {
		return ""
	}
	typeName, _ := tool["type"].(string)
	if typeName != "function" {
		return typeName
	}
	name, _ := tool["name"].(string)
	return typeName + ":" + name
}

func consoleRetryAfter(body []byte) time.Duration {
	text := string(body)
	index := strings.Index(strings.ToLower(text), "resets in:")
	if index < 0 {
		return 0
	}
	text = text[index+len("resets in:"):]
	var total time.Duration
	for _, match := range resetDurationPattern.FindAllStringSubmatch(text, -1) {
		value, _ := strconv.Atoi(match[1])
		switch strings.ToLower(match[2]) {
		case "d":
			total += time.Duration(value) * 24 * time.Hour
		case "h":
			total += time.Duration(value) * time.Hour
		case "m":
			total += time.Duration(value) * time.Minute
		case "s":
			total += time.Duration(value) * time.Second
		}
	}
	return total
}
