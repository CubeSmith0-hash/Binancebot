name: canli-pump

on:
  workflow_dispatch:
  schedule:
    - cron: "0 * * * *"

permissions:
  contents: write

concurrency:
  group: canli-pump
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    env:
      TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
      TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      LOOP_MINUTES: "330"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python canli.py
      - name: Kaydet
        if: always()
        run: |
          git config user.name "bot"
          git config user.email "bot@users.noreply.github.com"
          if [ -f canli_state.json ]; then git add canli_state.json; fi
          git commit -m "canli state" || exit 0
          for i in 1 2 3; do
            git pull --rebase && git push && break
            sleep 3
          done
