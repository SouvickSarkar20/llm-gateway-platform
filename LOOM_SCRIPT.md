# 5-Minute Loom Video Walkthrough Script

**Role Target:** AI/LLM Platform & DevOps Engineer  
**Target Duration:** 4 minutes 45 seconds (under 5:00 limit)

---

## Video Timeline & Presentation Cue Sheet

```
[00:00 - 00:45] Intro & Architectural Overview
[00:45 - 01:45] Code Walkthrough: Resilient LLM Gateway (Circuit Breaker & Fallback)
[01:45 - 02:45] Live Demo: Authentication, Cache Hit vs Miss, & Rate Limiting (429)
[02:45 - 03:45] Observability: Prometheus Metrics & PostgreSQL Usage/Cost Tracking
[03:45 - 04:30] Infrastructure & Deployment: Docker Compose, K8s HPA, & Migration
[04:30 - 04:45] Conclusion & Wrap-Up
```

---

### Segment 1: Introduction & Architecture (00:00 - 00:45)
- **Screen:** Show `README.md` or the Mermaid system diagram.
- **Script:**
  > *"Hi everyone, welcome! Today I’m walking you through this production-ready AI Question-Answering Platform. Rather than just wrapping an LLM API, I focused on the core engineering problems of running LLMs in production: gateway resilience, distributed rate limiting, token cost tracking, and zero-downtime scaling.*
  >
  > *Here is the high-level architecture: incoming traffic passes through JWT authentication and role-based access control, through a Redis sliding-window rate limiter, checks a response cache-aside layer, passes through our resilient LLM Gateway, and persists granular token usage and cost metrics into PostgreSQL before exporting real-time metrics to Prometheus."*

---

### Segment 2: Deep Dive into Resilient LLM Gateway (00:45 - 01:45)
- **Screen:** Open [app/services/llm_gateway.py](file:///d:/chat-assignment/app/services/llm_gateway.py).
- **Script:**
  > *"Let's look at the highest-leverage component: the Resilient LLM Gateway.
  >
  > First, we classify errors into retryable vs non-retryable. HTTP 429 rate limits, 5xx server errors, and network timeouts are retryable; 4xx client errors like bad prompts or malformed JSON fail immediately without wasting compute.
  >
  > For retries, we use exponential backoff with full randomized jitter to prevent the thundering herd problem against upstream providers.
  >
  > Notice this 3-state Circuit Breaker: `CLOSED`, `OPEN`, and `HALF-OPEN`. If a provider experiences 5 consecutive failures, the circuit trips to `OPEN`. Subsequent requests fast-fail immediately without making network calls, preventing hung threads.
  >
  > And most importantly, we don't just retry the same failed provider: if the primary model fails or trips the circuit, the gateway seamlessly fails over to our secondary model with zero interruption to the user."*

---

### Segment 3: Live Demo — Auth, Caching & Rate Limiting (01:45 - 02:45)
- **Screen:** Terminal running `curl` or Postman / Swagger UI at `http://localhost:8000/docs`.
- **Script:**
  > *"Now let's see it in action.
  >
  > 1. First, we call `/auth/login` to obtain our JWT token. Notice the token contains our RBAC claims.
  >
  > 2. Next, we hit `/chat` with a question: 'What is Kubernetes?'. On the first request, the response header shows `X-Cache: MISS`. The LLM generates the answer in 120ms and consumes 45 tokens.
  >
  > 3. If we submit the exact same question again: notice the header immediately returns `X-Cache: HIT`. Latency dropped to sub-5ms, and 0 tokens were consumed!
  >
  > 4. Now let’s hammer the endpoint past its quota: immediately, the distributed Redis rate limiter triggers HTTP 429 Too Many Requests with standard `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Reset` headers."*

---

### Segment 4: Observability & Persistent Auditing (02:45 - 03:45)
- **Screen:** Browser tab at `http://localhost:8000/metrics` and a terminal query on PostgreSQL.
- **Script:**
  > *"Next, observability. If we visit `/metrics`, this is not a mock JSON payload—it’s fully Prometheus-compatible. We expose custom counters for tokens by model, histograms for LLM execution latency, cache hit/miss counters, and a gauge tracking the exact state of our Circuit Breakers.
  >
  > In our database, every single request is audited in `llm_usage_logs`. We record the user ID, request ID, model, prompt and completion tokens, estimated USD cost, and whether it was served from cache or triggered a fallback. This provides tenant-level cost accounting out of the box."*

---

### Segment 5: DevOps & Scaling: 100 to 500 RPS (03:45 - 04:30)
- **Screen:** Open [k8s/hpa.yaml](file:///d:/chat-assignment/k8s/hpa.yaml) and [ARCHITECTURE.md](file:///d:/chat-assignment/ARCHITECTURE.md).
- **Script:**
  > *"From an infrastructure perspective, the entire application is containerized with a hardened, multi-stage non-root Dockerfile and deployed via Docker Compose or Kubernetes.
  >
  > In Section 4 and 5 of our architecture writeup, we model the scaling mathematics: at 500 RPS with a 35% cache hit rate and 2s p95 LLM latency, Little’s Law dictates 650 concurrent in-flight requests. With async Uvicorn workers handling 50 concurrent requests each, we autoscale between 7 and 20 pods using our Kubernetes HPA, scaling aggressively on CPU and queue depth while maintaining multi-AZ database replication and zero-downtime canary rollouts."*

---

### Segment 6: Wrap-Up (04:30 - 04:45)
- **Screen:** Show test terminal passing 100% tests.
- **Script:**
  > *"Every single component is validated with our automated pytest suite, covering retry backoff, circuit breaking, cache hits, and RBAC guards.
  >
  > Thank you for reviewing this project, and I look forward to discussing the architecture further!"*
