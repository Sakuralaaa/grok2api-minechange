package account

import (
	"net/http"
	"testing"
	"time"
)

func TestNormalizeBuildInspectionConfigDefaults(t *testing.T) {
	value := normalizeBuildInspectionConfig(BuildInspectionConfig{})
	if value.Interval != 6*time.Hour || value.Workers != 6 || value.QuotaCooldown != 24*time.Hour {
		t.Fatalf("automatic inspection defaults = %#v", value)
	}
	if value.QuotaAction != "cooldown" || value.ForbiddenAction != "refresh_then_delete" {
		t.Fatalf("automatic inspection actions = %#v", value)
	}
}

func TestClassifyBuildInspectionStatusPolicy(t *testing.T) {
	tests := []struct {
		name           string
		response       buildProbeResponse
		classification string
		action         string
	}{
		{name: "forbidden credential is dead", response: buildProbeResponse{Status: http.StatusForbidden, Body: []byte(`{"error":"access denied"}`)}, classification: "reauth", action: "delete"},
		{name: "payment required enters cooldown", response: buildProbeResponse{Status: http.StatusPaymentRequired, Body: []byte(`{"error":"quota exhausted"}`)}, classification: "quota_exhausted", action: "cooldown"},
		{name: "free usage exhaustion enters cooldown", response: buildProbeResponse{Status: http.StatusTooManyRequests, Body: []byte(`{"code":"subscription:free-usage-exhausted","error":"You've used all the included free usage"}`)}, classification: "quota_exhausted", action: "cooldown"},
		{name: "bare rate limit enters cooldown", response: buildProbeResponse{Status: http.StatusTooManyRequests, Body: []byte(`{"error":"too many requests"}`)}, classification: "rate_limited", action: "cooldown"},
		{name: "body error code 429 enters cooldown", response: buildProbeResponse{Status: http.StatusBadRequest, Body: []byte(`{"code":"429","error":"rate limit exceeded"}`)}, classification: "rate_limited", action: "cooldown"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			result := classifyBuildInspection(test.response, false)
			if result.Classification != test.classification || result.Action != test.action {
				t.Fatalf("classification=%q action=%q", result.Classification, result.Action)
			}
		})
	}
}
