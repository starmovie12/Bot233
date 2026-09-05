FROM python:3.11-slim

# ---------------------------------------------------------------------------
# TA-Lib's PYTHON wrapper needs TA-Lib's C LIBRARY already installed on the
# system — pip alone cannot provide this. This block builds that C library
# from source before any pip install runs. This is the single most common
# reason a Freqtrade Docker build fails on a fresh host, so it's worth this
# many lines of comment: if your build ever breaks here, this is why.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    automake \
    libtool \
    && rm -rf /var/lib/apt/lists/* \
    && wget -q https://github.com/ta-lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz \
    && tar -xzf ta-lib-0.6.4-src.tar.gz \
    && cd ta-lib-0.6.4 \
    && ( [ -f ./configure ] || ( [ -f ./autogen.sh ] && chmod +x ./autogen.sh && ./autogen.sh ) ) \
    && ./configure --prefix=/usr \
    && make \
    && make install \
    && cd .. \
    && rm -rf ta-lib-0.6.4 ta-lib-0.6.4-src.tar.gz

WORKDIR /freqtrade

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY user_data/ /freqtrade/user_data/

# Render sets $PORT at runtime; Freqtrade's api_server.listen_port in
# config.json is what actually matters for where it binds. We expose 8080
# to match config.json's api_server.listen_port default — see
# DEPLOY_GUIDE.md if you need to change the port Render routes to.
EXPOSE 8080

# --db-url and the API username/password are passed as environment
# variables at container-start (see start.sh) rather than baked into
# config.json, so secrets never sit in plaintext in the image or in git.
COPY start.sh /freqtrade/start.sh
RUN chmod +x /freqtrade/start.sh

ENTRYPOINT ["/freqtrade/start.sh"]
