/**
 * AgentValue 开放 API 的 TypeScript SDK（WS-3）
 *
 * 对标 Stripe / Svix 官方 SDK：
 * - fetch 客户端（内置 429 / 5xx 指数退避重试，`X-API-Key` 鉴权）；
 * - Webhook 签名校验（与平台 `services/webhook_delivery_service.py` 的
 *   HMAC-SHA256 配方一致：`t=...,v1=...` 签名头，`f"{t}.{raw_body}"` 签名对象）。
 *
 * 用法：
 * ```ts
 * import { AgentValueClient, verifyWebhookSignature } from "@agentvalue/sdk";
 *
 * const client = new AgentValueClient({ baseUrl: "https://av.example.com", apiKey: "ak_xxx" });
 * const me = await client.getMe();
 * const ok = verifyWebhookSignature({ secret, timestamp, body, signature });
 * ```
 */

import { createHmac, timingSafeEqual } from "node:crypto";

/** 平台默认签名时间戳容忍窗口（秒），与服务端 SIGNATURE_TOLERANCE_SECONDS 一致 */
export const SIGNATURE_TOLERANCE_SECONDS = 300;

export interface ClientOptions {
  baseUrl: string;
  apiKey: string;
  /** 单次请求超时（ms），默认 30_000 */
  timeoutMs?: number;
  /** 429 / 5xx 最大重试次数，默认 5 */
  maxRetries?: number;
  /** 退避基数（s），默认 1 */
  backoffBase?: number;
  /** 退避倍数，默认 2 */
  backoffFactor?: number;
}

export interface VerifyOptions {
  secret: string;
  /** 请求到达时的 unix 秒（防重放）；若签名头含 `t=` 则以头内值为准 */
  timestamp: number;
  /** 原始请求体字符串（未经反序列化/重新序列化） */
  body: string;
  /** `X-AgentValue-Signature` 头原值（`t=...,v1=...`），或仅 v1 hex */
  signature: string;
  /** 容忍窗口（s），<=0 表示不校验时间 */
  toleranceSeconds?: number;
  /** 覆盖当前时间（unix 秒），测试用 */
  now?: number;
}

export class AgentValueError extends Error {}

/** 平台返回 4xx（不重试） */
export class ApiError extends AgentValueError {
  constructor(
    public readonly statusCode: number,
    public readonly detail: unknown,
  ) {
    super(`AgentValue API 错误: HTTP ${statusCode} ${JSON.stringify(detail)}`);
    this.name = "ApiError";
  }
}

/** 429 / 5xx 重试耗尽 */
export class RetryableError extends AgentValueError {
  constructor(message: string) {
    super(message);
    this.name = "RetryableError";
  }
}

// ---------------------------------------------------------------------------
// Webhook 签名校验
// ---------------------------------------------------------------------------

function parseSignature(header: string): Record<string, string> {
  const parsed: Record<string, string> = {};
  if (!header) return parsed;
  for (const part of header.split(",")) {
    const idx = part.indexOf("=");
    if (idx < 0) continue;
    parsed[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  }
  return parsed;
}

function buildSignature(secret: string, timestamp: number, body: string): string {
  const digest = createHmac("sha256", secret).update(`${timestamp}.${body}`).digest("hex");
  return `t=${timestamp},v1=${digest}`;
}

/**
 * 校验 webhook 签名（防重放 + 防篡改）。
 * 返回 true 表示签名有效且时间戳在容忍窗口内。
 */
export function verifyWebhookSignature(options: VerifyOptions): boolean {
  const { secret, body, signature, timestamp } = options;
  const tolerance = options.toleranceSeconds ?? SIGNATURE_TOLERANCE_SECONDS;
  const parsed = parseSignature(signature);
  let eventTs = timestamp;
  const rawTs = parsed["t"];
  if (rawTs !== undefined) {
    const n = Number(rawTs);
    if (!Number.isFinite(n)) return false;
    eventTs = Math.trunc(n);
  }
  let provided = parsed["v1"];
  if (!provided) provided = signature.trim();
  if (!provided) return false;

  if (tolerance > 0) {
    const current = options.now ?? Math.floor(Date.now() / 1000);
    if (Math.abs(current - eventTs) > tolerance) return false;
  }

  const expectedV1 = parseSignature(buildSignature(secret, eventTs, body))["v1"] ?? "";
  const a = Buffer.from(expectedV1, "utf8");
  const b = Buffer.from(provided, "utf8");
  return a.length === b.length && timingSafeEqual(a, b);
}

// ---------------------------------------------------------------------------
// fetch 客户端
// ---------------------------------------------------------------------------

function shouldRetry(status: number): boolean {
  return status === 429 || status >= 500;
}

function backoffDelay(attempt: number, base: number, factor: number): number {
  return base * Math.pow(factor, attempt) + Math.random() * 0.2;
}

export class AgentValueClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly backoffBase: number;
  private readonly backoffFactor: number;

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.maxRetries = options.maxRetries ?? 5;
    this.backoffBase = options.backoffBase ?? 1.0;
    this.backoffFactor = options.backoffFactor ?? 2.0;
  }

  private path(route: string): string {
    return `${this.baseUrl}/api/public/v1${route}`;
  }

  private headers(): Record<string, string> {
    return { Accept: "application/json", "X-API-Key": this.apiKey };
  }

  private async request<T>(
    method: string,
    route: string,
    opts: { params?: Record<string, unknown>; json?: unknown } = {},
  ): Promise<T> {
    const url = new URL(this.path(route));
    if (opts.params) {
      for (const [key, value] of Object.entries(opts.params)) {
        if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
      }
    }
    const init: RequestInit = {
      method,
      headers: this.headers(),
      signal: AbortSignal.timeout(this.timeoutMs),
    };
    if (opts.json !== undefined) {
      init.headers = { ...init.headers, "Content-Type": "application/json" };
      init.body = JSON.stringify(opts.json);
    }

    let lastError: RetryableError | null = null;
    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      let response: Response;
      try {
        response = await fetch(url.toString(), init);
      } catch (err) {
        throw new AgentValueError(`网络请求失败: ${String(err)}`);
      }
      if (response.ok) {
        const text = await response.text();
        return (text ? JSON.parse(text) : null) as T;
      }
      if (shouldRetry(response.status)) {
        lastError = new RetryableError(`HTTP ${response.status}`);
        if (attempt < this.maxRetries) {
          const delayMs = backoffDelay(attempt, this.backoffBase, this.backoffFactor) * 1000;
          await new Promise((resolve) => setTimeout(resolve, delayMs));
          continue;
        }
        throw lastError;
      }
      const raw = await response.text();
      let detail: unknown = raw;
      try {
        detail = JSON.parse(raw);
      } catch {
        /* 非 JSON 响应体原样返回 */
      }
      throw new ApiError(response.status, detail);
    }
    throw lastError ?? new RetryableError("请求失败");
  }

  // ---- 身份自省 ----
  getMe(): Promise<Record<string, unknown>> {
    return this.request("GET", "/me");
  }

  // ---- 评估 ----
  createEvaluation(
    employeeId: string,
    period: string,
    rawInputs?: Array<Record<string, unknown>>,
  ): Promise<Record<string, unknown>> {
    return this.request("POST", "/evaluations", {
      json: { employee_id: employeeId, period, ...(rawInputs ? { raw_inputs: rawInputs } : {}) },
    });
  }

  listEvaluations(params: {
    employee_id?: string;
    status?: string;
    period?: string;
    page?: number;
    page_size?: number;
  } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/evaluations", { params });
  }

  getEvaluation(evaluationId: string): Promise<Record<string, unknown>> {
    return this.request("GET", `/evaluations/${evaluationId}`);
  }

  // ---- Agent ----
  listAgents(params: { category?: string; page?: number; page_size?: number } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/agents", { params });
  }

  invokeAgent(agentId: number, input: string, context?: string): Promise<Record<string, unknown>> {
    return this.request("POST", `/agents/${agentId}/invoke`, {
      json: { input, ...(context !== undefined ? { context } : {}) },
    });
  }

  // ---- 数据集 ----
  listDatasets(params: { dataset_type?: string; page?: number; page_size?: number } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/datasets", { params });
  }

  listDatasetItems(datasetId: number, params: { status?: string; page?: number; page_size?: number } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", `/datasets/${datasetId}/items`, { params });
  }

  // ---- 链路追踪 ----
  listTraces(params: { kind?: string; status?: string; page?: number; page_size?: number } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/traces", { params });
  }
}

export default AgentValueClient;
