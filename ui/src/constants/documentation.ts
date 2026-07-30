import { docsUrl } from "@/lib/support";

// Every value below is `null` when no documentation site is configured
// (see `@/lib/support` for why that is the default). Call sites must hide the
// link in that case rather than rendering a dead href.

export const NODE_DOCUMENTATION_URLS: Record<string, string | null> = {
    startCall: docsUrl("voice-agent/start-call"),
    endCall: docsUrl("voice-agent/end-call"),
    agent: docsUrl("voice-agent/agent"),
    global: docsUrl("voice-agent/global"),
    apiTrigger: docsUrl("voice-agent/api-trigger"),
    webhook: docsUrl("voice-agent/webhook"),
    qaAnalysis: docsUrl("getting-started"),
};

export const CONTEXT_VARIABLES_DOC_URL = docsUrl("core-concepts/context-and-variables");

export const TOOLS_INTRODUCTION_DOC_URL = docsUrl("voice-agent/tools/introduction");

export const KNOWLEDGE_BASE_DOC_URL = docsUrl("voice-agent/knowledge-base");

export const PRE_CALL_DATA_FETCH_DOC_URL = docsUrl("voice-agent/pre-call-data-fetch");

// Deliberately not annotated as `Record<string, …>`: the keys are fixed and
// call sites narrow with `{URL && <a …>}`, which TypeScript only does reliably
// for declared properties, not for index-signature accesses.
export const SETTINGS_DOCUMENTATION_URLS = {
    general: docsUrl("voice-agent/editing-a-workflow"),
    modelOverrides: docsUrl("configurations/inference-providers"),
    templateVariables: docsUrl("voice-agent/template-variables"),

    recordings: docsUrl("voice-agent/pre-recorded-audio"),
    deployment: docsUrl("voice-agent/add-to-website"),
};

export const WIDGET_MODE_DOCUMENTATION_URLS: Record<"floating" | "inline" | "headless", string | null> = {
    floating: docsUrl("voice-agent/add-to-website#floating-widget"),
    inline: docsUrl("voice-agent/add-to-website#inline-component"),
    headless: docsUrl("voice-agent/add-to-website#headless-mode"),
};

export const TOOL_DOCUMENTATION_URLS: Record<string, string | null> = {
    http_api: docsUrl("voice-agent/tools/http-api"),
    end_call: docsUrl("voice-agent/tools/end-call"),
    transfer_call: docsUrl("voice-agent/tools/call-transfer"),
};
