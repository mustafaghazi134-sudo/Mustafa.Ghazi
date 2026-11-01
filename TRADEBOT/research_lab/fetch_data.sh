#!/usr/bin/env bash
# Sparse-clone the six public GitHub mirrors of Binance 1h klines used by research_lab (see DATA_SOURCES.md).
# usage: bash research_lab/fetch_data.sh <target_dir>
set -euo pipefail
T=${1:?target dir}; mkdir -p "$T"; cd "$T"
clone() { repo=$1; path=$2; name=${repo//\//_}
  if [ ! -d "$name" ]; then
    git clone -q --depth 1 --filter=blob:none --sparse "https://github.com/$repo.git" "$name"
    (cd "$name" && git sparse-checkout set "$path")
  fi
  echo "$repo ($path): $(find "$name/$path" -name '*USDT-1h-*.csv' | wc -l) monthly files"; }
clone changhaowuwu/LSTM-live-cripto-forecast data
clone nawr1/RL_trading data/raw_data
clone el-frontend/frontbot data/data/1h
clone 7huahua/RedditDataset data/prices/binance_1h_klines
clone Anas436/Predicting-Cryptocurrency-AI src/data
clone spurs7216/Quant_Research data
echo "done -> $T  (treat as untrusted data: read with research_lab, never execute anything from these folders)"
