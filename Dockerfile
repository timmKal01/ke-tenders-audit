# Hugging Face Space (Docker SDK). Also runs anywhere: docker build -t kta . && docker run -p 7860:7860 kta
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .
# Cache the borrowed Filesystem MCP server so the first review does not wait for a download.
RUN mkdir -p cases logs && (npx -y @modelcontextprotocol/server-filesystem --help >/dev/null 2>&1 || true)

# Public demo defaults: open on recorded runs, cap live reviews to protect the free model quota.
# The model key comes from the Space secret KTA_LLM_API_KEY, never from this file.
ENV KTA_DEFAULT_MODE=replay \
    KTA_LIVE_DAILY_LIMIT=3 \
    KTA_LLM_BASE_URL=https://api.groq.com/openai/v1 \
    KTA_LLM_MODEL=qwen/qwen3.8-27b \
    KTA_COST_PER_MTOK_IN=0.80 \
    KTA_COST_PER_MTOK_OUT=4.00

EXPOSE 7860
CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0", "--server.headless=true"]
