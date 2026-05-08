# 🐋 Whale Watch

SEC 13F 공시를 매일 자동으로 받아와 미국의 슈퍼투자자 10명의 포트폴리오를 보여주는 정적 웹사이트.

## 추적 대상 (10명)

| 스타일 | 투자자 | 펀드 |
| --- | --- | --- |
| 가치투자형 | Warren Buffett | Berkshire Hathaway |
| 가치투자형 | Bill Ackman | Pershing Square Capital |
| 가치투자형 | Seth Klarman | Baupost Group |
| 가치투자형 | Li Lu | Himalaya Capital |
| 가치투자형 | Mohnish Pabrai | Pabrai Investments |
| 매크로·헤지펀드형 | Ray Dalio | Bridgewater Associates |
| 매크로·헤지펀드형 | Stanley Druckenmiller | Duquesne Family Office |
| 매크로·헤지펀드형 | David Tepper | Appaloosa Management |
| 역발상·공격형 | Michael Burry | Scion Asset Management |
| 역발상·공격형 | Cathie Wood | ARK Invest |

## 구조

```
whale-watch/
├── index.html                # 단일 페이지 앱 (CSS/JS 인라인)
├── data/
│   ├── investors.json        # 메타데이터 + 보유 종목 (자동 갱신)
│   └── cusip_cache.json      # CUSIP → 티커 변환 캐시
├── scripts/
│   └── fetch_13f.py          # EDGAR에서 최신 13F 받아 JSON 재빌드
├── requirements.txt
└── .github/workflows/
    ├── update-13f.yml        # 매일 11:30 UTC 자동 갱신
    └── pages.yml             # main 푸시 시 GitHub Pages 배포
```

## 로컬 실행

정적 사이트라 로컬 서버만 띄우면 됩니다. `data/investors.json`을 fetch하기 때문에 file:// 경로로는 동작 안 합니다.

```bash
cd whale-watch
python -m http.server 8000
# 브라우저에서 http://localhost:8000
```

## 데이터 갱신 (수동)

```bash
pip install -r requirements.txt

# SEC EDGAR는 User-Agent 헤더에 연락처(이메일)를 요구합니다
export EDGAR_USER_AGENT="Whale Watch your-email@example.com"

# (선택) OpenFIGI API 키가 있으면 CUSIP → 티커 변환 속도가 빨라집니다
# 없어도 동작합니다 (rate limit만 걸림)
export OPENFIGI_API_KEY=""

python scripts/fetch_13f.py
```

스크립트 동작:
1. `data/investors.json`의 CIK 목록을 읽음
2. 각 투자자별로 SEC submissions API → 최신 13F-HR 파일링 조회
3. `informationTable.xml` 다운로드 후 보유 종목 파싱
4. CUSIP → 티커 변환 (OpenFIGI, 캐시 사용)
5. `data/investors.json` 재작성 + `last_updated` 갱신

## GitHub Pages 배포

1. 새 GitHub 레포 만들고 이 폴더 그대로 푸시
2. Repo → Settings → Pages → **Source: GitHub Actions** 선택
3. Repo → Settings → Secrets and variables → Actions → New secret
   - `EDGAR_USER_AGENT` = `Whale Watch your-email@example.com` (필수)
   - `OPENFIGI_API_KEY` = (선택, [openfigi.com](https://www.openfigi.com/api)에서 무료 발급)
4. main 브랜치에 푸시되면 자동 배포됨 (`pages.yml`)
5. 매일 11:30 UTC에 13F 자동 갱신 (`update-13f.yml`) — 변경 시 커밋·푸시 → 재배포

수동 갱신은 Actions 탭에서 "Update 13F holdings" → Run workflow.

## 13F 공시의 한계

화면 하단에도 표시되지만 한 번 더:

- **분기 종료 후 최대 45일 지연** — 실시간이 아닙니다
- **미국 상장 롱 포지션만** 포함 (공매도·현금·해외주식·채권 제외)
- 옵션(풋/콜)은 본 사이트에서 별도 제외 (혼란 방지)
- 공시 시점과 실제 보유 시점이 달라 이미 매도한 종목이 포함될 수 있음

## CIK 검증

`data/investors.json`의 CIK가 잘못된 경우 fetch 스크립트가 "no 13F-HR found"를 출력합니다. SEC EDGAR 검색에서 펀드명으로 직접 확인 가능:

> https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<펀드명>&type=13F

## 라이선스

코드는 MIT. 13F 데이터는 SEC 퍼블릭 도메인.
