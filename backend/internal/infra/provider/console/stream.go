package console

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"
)

const syntheticReasoningText = "已深度思考。"

// TransformStream rewrites Responses SSE model fields to publicID and injects
// synthetic reasoning events after the first JSON data frame.
func TransformStream(source io.ReadCloser, publicID string) io.ReadCloser {
	return TransformStreamWithOptions(source, publicID, true)
}

func TransformStreamWithOptions(source io.ReadCloser, publicID string, synthesizeReasoning bool) io.ReadCloser {
	if source == nil {
		return source
	}
	publicID = strings.TrimSpace(publicID)
	reader, writer := io.Pipe()
	go func() {
		defer source.Close()
		err := transformConsoleStream(writer, source, publicID, synthesizeReasoning)
		_ = writer.CloseWithError(err)
	}()
	return reader
}

// HeartbeatStream emits SSE comment heartbeats while the source is idle.
// interval <= 0 disables heartbeats and returns source unchanged.
func HeartbeatStream(source io.ReadCloser, interval time.Duration) io.ReadCloser {
	if source == nil || interval <= 0 {
		return source
	}
	reader, writer := io.Pipe()
	go func() {
		defer source.Close()
		err := copyWithHeartbeat(writer, source, interval)
		_ = writer.CloseWithError(err)
	}()
	return reader
}

// WrapStream applies TransformStream then HeartbeatStream (Responses path helper).
func WrapStream(source io.ReadCloser, publicID string, heartbeatInterval time.Duration) io.ReadCloser {
	return HeartbeatStream(TransformStream(source, publicID), heartbeatInterval)
}

func transformConsoleStream(dst io.Writer, source io.Reader, publicID string, synthesizeReasoning bool) error {
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	syntheticSent := false
	reasoningID := "rs_" + randomHex(12)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.TrimSpace(line) == "" {
			if _, err := io.WriteString(dst, "\n"); err != nil {
				return err
			}
			continue
		}
		if isSSEComment(line) {
			if _, err := io.WriteString(dst, strings.TrimRight(line, "\r")+"\n\n"); err != nil {
				return err
			}
			continue
		}
		out := rewriteResponseSSELine(line, publicID)
		if _, err := io.WriteString(dst, out+"\n\n"); err != nil {
			return err
		}
		if synthesizeReasoning && !syntheticSent && isJSONDataLine(line) {
			for _, event := range syntheticResponseReasoningEvents(reasoningID) {
				if _, err := io.WriteString(dst, event); err != nil {
					return err
				}
			}
			syntheticSent = true
		}
	}
	return scanner.Err()
}

func copyWithHeartbeat(dst io.Writer, source io.Reader, interval time.Duration) error {
	type item struct {
		chunk []byte
		err   error
		done  bool
	}
	ch := make(chan item, 8)
	go func() {
		buf := make([]byte, 32*1024)
		for {
			n, err := source.Read(buf)
			if n > 0 {
				cp := make([]byte, n)
				copy(cp, buf[:n])
				ch <- item{chunk: cp}
			}
			if err != nil {
				if err == io.EOF {
					ch <- item{done: true}
					return
				}
				ch <- item{err: err}
				return
			}
		}
	}()

	for {
		timer := time.NewTimer(interval)
		select {
		case payload := <-ch:
			if !timer.Stop() {
				<-timer.C
			}
			if payload.err != nil {
				return payload.err
			}
			if payload.done {
				return nil
			}
			if _, err := dst.Write(payload.chunk); err != nil {
				return err
			}
		case <-timer.C:
			if _, err := io.WriteString(dst, ": ping\n\n"); err != nil {
				return err
			}
		}
	}
}

// NormalizeResponseJSON rewrites model to publicID and injects synthetic reasoning when missing.
func NormalizeResponseJSON(data []byte, publicID string) ([]byte, error) {
	publicID = strings.TrimSpace(publicID)
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return data, nil
	}
	var obj map[string]any
	if err := json.Unmarshal(trimmed, &obj); err != nil {
		return data, nil
	}
	normalizeResponseObject(obj, publicID, true)
	return json.Marshal(obj)
}

func normalizeResponseObject(obj map[string]any, publicID string, emitThink bool) {
	if publicID != "" {
		obj["model"] = publicID
	}
	if emitThink {
		injectSyntheticResponseReasoning(obj)
	}
	if response, ok := obj["response"].(map[string]any); ok {
		if publicID != "" {
			response["model"] = publicID
		}
		if emitThink {
			injectSyntheticResponseReasoning(response)
		}
	}
}

func injectSyntheticResponseReasoning(obj map[string]any) {
	output, ok := obj["output"].([]any)
	if !ok {
		if raw, exists := obj["output"]; exists && raw != nil {
			return
		}
		output = []any{}
	}
	for _, item := range output {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if m["type"] != "reasoning" {
			continue
		}
		if extractReasoningText(m) == "" {
			m["summary"] = []any{map[string]any{"type": "summary_text", "text": syntheticReasoningText}}
		}
		return
	}
	obj["output"] = append([]any{syntheticResponseReasoningItem("rs_" + randomHex(12))}, output...)
}

func extractReasoningText(item map[string]any) string {
	summary, ok := item["summary"].([]any)
	if !ok {
		return ""
	}
	var b strings.Builder
	for _, part := range summary {
		m, ok := part.(map[string]any)
		if !ok {
			continue
		}
		if text, ok := m["text"].(string); ok {
			b.WriteString(text)
		}
	}
	return b.String()
}

func syntheticResponseReasoningItem(reasoningID string) map[string]any {
	return map[string]any{
		"id":     reasoningID,
		"type":   "reasoning",
		"status": "completed",
		"summary": []any{
			map[string]any{"type": "summary_text", "text": syntheticReasoningText},
		},
	}
}

func syntheticResponseReasoningEvents(reasoningID string) []string {
	events := []map[string]any{
		{
			"type": "response.output_item.added", "output_index": 0,
			"item": map[string]any{"id": reasoningID, "type": "reasoning", "summary": []any{}, "status": "in_progress"},
		},
		{
			"type": "response.reasoning_summary_part.added", "item_id": reasoningID, "output_index": 0, "summary_index": 0,
			"part": map[string]any{"type": "summary_text", "text": ""},
		},
		{
			"type": "response.reasoning_summary_text.delta", "item_id": reasoningID, "output_index": 0, "summary_index": 0,
			"delta": syntheticReasoningText,
		},
		{
			"type": "response.reasoning_summary_text.done", "item_id": reasoningID, "output_index": 0, "summary_index": 0,
			"text": syntheticReasoningText,
		},
		{
			"type": "response.reasoning_summary_part.done", "item_id": reasoningID, "output_index": 0, "summary_index": 0,
			"part": map[string]any{"type": "summary_text", "text": syntheticReasoningText},
		},
		{
			"type": "response.output_item.done", "output_index": 0,
			"item": syntheticResponseReasoningItem(reasoningID),
		},
	}
	out := make([]string, 0, len(events))
	for _, event := range events {
		data, err := json.Marshal(event)
		if err != nil {
			continue
		}
		out = append(out, fmt.Sprintf("event: %s\ndata: %s\n\n", event["type"], data))
	}
	return out
}

func rewriteResponseSSELine(line, publicID string) string {
	if publicID == "" || !strings.HasPrefix(line, "data:") {
		return strings.TrimRight(line, "\r")
	}
	data := strings.TrimSpace(line[5:])
	if data == "" || data == "[DONE]" || !strings.HasPrefix(data, "{") {
		return strings.TrimRight(line, "\r")
	}
	var obj map[string]any
	if err := json.Unmarshal([]byte(data), &obj); err != nil {
		return strings.TrimRight(line, "\r")
	}
	normalizeResponseObject(obj, publicID, false)
	encoded, err := json.Marshal(obj)
	if err != nil {
		return strings.TrimRight(line, "\r")
	}
	return "data: " + string(encoded)
}

func isJSONDataLine(line string) bool {
	if !strings.HasPrefix(line, "data:") {
		return false
	}
	data := strings.TrimSpace(line[5:])
	return data != "" && data != "[DONE]" && strings.HasPrefix(data, "{")
}

func isSSEComment(line string) bool {
	return strings.HasPrefix(strings.TrimLeft(line, " \t"), ":")
}

func randomHex(n int) string {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)[:n]
}


// EnsureChatReasoningAliases adds reasoning/thinking aliases expected by some clients.
func EnsureChatReasoningAliases(data []byte) []byte {
	var obj map[string]any
	if err := json.Unmarshal(data, &obj); err != nil {
		return data
	}
	choices, ok := obj["choices"].([]any)
	if !ok || len(choices) == 0 {
		return data
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return data
	}
	msg, ok := choice["message"].(map[string]any)
	if !ok {
		return data
	}
	content, _ := msg["reasoning_content"].(string)
	if content == "" {
		return data
	}
	if _, exists := msg["reasoning"]; !exists {
		msg["reasoning"] = content
	}
	if _, exists := msg["thinking"]; !exists {
		msg["thinking"] = content
	}
	encoded, err := json.Marshal(obj)
	if err != nil {
		return data
	}
	return encoded
}
