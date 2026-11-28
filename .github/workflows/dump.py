name: canli-dump

on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:

concurrency:
  group: canli-dump
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Bağımlılıklar
        run: pip install requests

      - name: Dump botu
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          LOOP_MINUTES: "330"
        run: python canli_dump.py

      - name: State'i kaydet
        if: always()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add canli_dump_state.json || true
          if git diff --cached --quiet; then
            echo "state değişmedi"
            exit 0
          fi
          git commit -m "dump state [skip ci]"
          for i in 1 2 3 4 5; do
            git pull --rebase -X theirs origin "${GITHUB_REF_NAME}" || git rebase --abort
            if git push origin "HEAD:${GITHUB_REF_NAME}"; then
              exit 0
            fi
            sleep $((i * 3))
          done
          echo "push başarısız"
          exit 1
