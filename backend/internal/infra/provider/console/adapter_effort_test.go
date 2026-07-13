package console

import (
	"encoding/json"
	"testing"
)

func TestEnrichConsoleBodyPinsNamedModelEffort(t *testing.T) {
	adapter := NewAdapter(Config{}, nil, nil)
	tests := []struct {
		publicID      string
		upstreamModel string
		inputEffort   string
		wantEffort    string
	}{
		{publicID: "grok-4.3-low-console", upstreamModel: "grok-4.3", inputEffort: "xhigh", wantEffort: "low"},
		{publicID: "grok-4.3-medium-console", upstreamModel: "grok-4.3", inputEffort: "low", wantEffort: "medium"},
		{publicID: "grok-4.3-high-console", upstreamModel: "grok-4.3", inputEffort: "low", wantEffort: "high"},
		{publicID: "grok-4.20-heavy-low-console", upstreamModel: "grok-4.20-multi-agent-0309", inputEffort: "xhigh", wantEffort: "low"},
		{publicID: "grok-4.20-heavy-medium-console", upstreamModel: "grok-4.20-multi-agent-0309", inputEffort: "low", wantEffort: "medium"},
		{publicID: "grok-4.20-heavy-high-console", upstreamModel: "grok-4.20-multi-agent-0309", inputEffort: "low", wantEffort: "high"},
		{publicID: "grok-4.20-heavy-xhigh-console", upstreamModel: "grok-4.20-multi-agent-0309", inputEffort: "low", wantEffort: "xhigh"},
	}
	for _, test := range tests {
		t.Run(test.publicID, func(t *testing.T) {
			body := []byte(`{"reasoning":{"effort":"` + test.inputEffort + `"}}`)
			enriched, err := adapter.enrichConsoleBody(body, test.publicID, test.upstreamModel, false)
			if err != nil {
				t.Fatal(err)
			}
			if got := decodedEffort(t, enriched); got != test.wantEffort {
				t.Fatalf("reasoning effort = %q, want %q; body=%s", got, test.wantEffort, enriched)
			}
		})
	}
}

func TestEnrichConsoleBodyUsesClientOrMediumForUnpinnedReasoningModel(t *testing.T) {
	adapter := NewAdapter(Config{}, nil, nil)

	enriched, err := adapter.enrichConsoleBody([]byte(`{}`), "grok-4.3-console", "grok-4.3", false)
	if err != nil {
		t.Fatal(err)
	}
	if got := decodedEffort(t, enriched); got != "medium" {
		t.Fatalf("default reasoning effort = %q, want medium", got)
	}

	enriched, err = adapter.enrichConsoleBody([]byte(`{"reasoning":{"effort":"high"}}`), "grok-4.3-console", "grok-4.3", false)
	if err != nil {
		t.Fatal(err)
	}
	if got := decodedEffort(t, enriched); got != "high" {
		t.Fatalf("client reasoning effort = %q, want high", got)
	}
}

func TestEnrichConsoleBodyDoesNotInferLowFromSharedUpstream(t *testing.T) {
	adapter := NewAdapter(Config{}, nil, nil)
	enriched, err := adapter.enrichConsoleBody([]byte(`{"reasoning":{"effort":"xhigh"}}`), "unknown-console-model", "grok-4.20-multi-agent-0309", false)
	if err != nil {
		t.Fatal(err)
	}
	if got := decodedEffort(t, enriched); got != "xhigh" {
		t.Fatalf("unknown public model effort = %q, want xhigh", got)
	}
}

func decodedEffort(t *testing.T, body []byte) string {
	t.Helper()
	var payload struct {
		Reasoning struct {
			Effort string `json:"effort"`
		} `json:"reasoning"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		t.Fatal(err)
	}
	return payload.Reasoning.Effort
}
