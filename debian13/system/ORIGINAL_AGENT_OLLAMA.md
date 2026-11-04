# Cztery oryginalne AI + lokalna Ollama (integracja bez automatycznych płatności)

## Status

Ten moduł jest proponowaną integracją na gałęzi PR #14, współpracującą
z **PR #19** w `u6930438163-code/AI_Studio_BastillaOS`.
PR #19 został zamknięty bez scalenia; jego poprawki nie są wdrożone przez samo istnienie tej integracji.
Dopóki zgodne wersje obu usług nie zostaną świadomie wdrożone oraz nie zostanie wykonany
test z prawdziwym serwerem, **nie ma potwierdzonej pracy agentów**.

**Nie wymaga OpenAI API ani klucza OpenAI**. Dedykowany worker źródłowy
wymusza `providers: ['ollama']` niezależnie od konfiguracji głównego
czatu Bastilla. Nie uruchamia płatnego Gemini, xAI ani OpenAI.
Ollama wymaga hosta (komputer, VPS lub serwer) i odpowiedniego modelu.
Płatny fallback OpenAI jest wyłączony i niezaimplementowany. API OpenAI
ma osobne rozliczenie od subskrypcji ChatGPT.

## Źródłowy serwer (AI Studio BastillaOS)

W bezpiecznych, prywatnych zmiennych środowiskowych (NIE w repozytorium):

- `DEBIAN13_ORIGINAL_WORKERS_ENABLED=1` — opt-in dopiero po uruchomieniu Ollamy.
- `DEBIAN13_ORIGINAL_AGENTS_TOKEN` — nowy, losowy token (min. 32 znaki).
- `LLM_PROVIDERS=ollama` — zalecane dla źródłowego serwera, zwłaszcza
  gdy nie chcesz opłacać modeli chmurowych w pozostałych modułach.
- `OLLAMA_MODEL` — niewielki, rzeczywiście pobrany model (dobierz do RAM).
- Lokalna Ollama powinna być osiągalna wyłącznie w zaufanej sieci hosta.

Nie używaj w tym celu niebezpiecznego `/api/triad/message` ze starszej
aplikacji: mógł wykonywać polecenia. Integracja obsługuje **tylko**
uwierzytelnioną kolejkę `/api/agent-tasks/*` (review bez shella).

## Serwer Debian 13

W sekretnych zmiennych środowiskowych po stronie API Debiana 13:

- `ALEX_EXISTING_API_URL=https://twoj-prywatny-serwer.example/api`
  lub `http://127.0.0.1:3000/api` przy działaniu obu usług na tym samym hoście.
- `ORIGINAL_AGENTS_API_TOKEN` — **ta sama wartość**, co
  `DEBIAN13_ORIGINAL_AGENTS_TOKEN` w źródłowym serwerze. Nigdy nie
  umieszczaj jej w wiadomości ChatGPT, GitHub Issue ani kodzie.
- `ORIGINAL_AGENTS_ALLOW_REVIEW=1` — jawne uruchomienie przekazywania
  zadań przez operatora po weryfikacji konfiguracji.

Następnie uruchom prywatną konsolę Debian 13 i otwórz zakładkę Work.
Utwórz zadanie z wybranym agentem, kliknij „Wyślij do AI”, a potem
„Sprawdź oryginalne AI”. API sprawdza rzeczywiste zdolności serwera:
`debian13_review_task_v1`, cztery nazwy, identyczne docelowe repo i
brak domyślnych ról lub custom instructions.

Stany `queued` i `running` **nie potwierdzają odpowiedzi modelu**.
Dopiero `completed` z nazwą providera, modelu i zapisaną treścią
dokumentuje wygenerowaną analizę. **Analiza nie jest commitem, PR,
rootem, zakupem ani ukończeniem projektu.** Do prawdziwego programowania
przez AI potrzebny będzie oddzielny kontrolowany worker GitHub z testami.

## Kontrole bezpieczeństwa

- Wystawianie publicznego HTTP i automatyczne przekazywanie kluczy zabronione.
- Zdalny serwer wyłącznie HTTPS z autoryzacją lub lokalna pętla loopback.
- Brak autoryzacji lub niezgodność celu repo => zatrzymanie przekazania.
- Brak automatycznego scalenia, zakupów, shella i opłat za fallback.
- Oryginalna czwórka startuje bez przydzielonych ról i promptów.

## GitHub: oryginalni agenci zamiast profili ogólnego modelu

Workflow `.github/workflows/debian13-agent-task.yml` na tej gałęzi korzysta
z `original_agent_plan.py` i istniejącego handoffu do oryginalnego API.
Nie tworzy person o tych samych imionach i nie wywołuje ogólnego endpointu
OpenAI-compatible. Wybór agenta: alex, aria, astyria lub aurelius.
Zadanie uruchamiane jest ręcznie, a analiza zapisywana jako artefakt
`original-ai-analysis` przez 7 dni. Kod modelu nie jest wykonywany.

W Settings → Secrets and variables → Actions skonfiguruj prywatnie:

- `DEBIAN13_ORIGINAL_AGENTS_API_URL`: osiągalny z runnera HTTPS URL
  oryginalnego API, zakończony `/api`;
- `DEBIAN13_ORIGINAL_AGENTS_TOKEN`: token zgodnego źródłowego serwera.

Nie przenoś tokenów do YAML ani Issue. Brak sekretów, niezgodne capability,
błąd workera, timeout lub brak zweryfikowanej odpowiedzi zatrzymują zadanie.
Workflow przyjmuje wyłącznie ukończony raport oznaczony `provider=ollama`.
To kontrola odpowiedzi, nie mechanizm blokowania kosztów na zdalnym serwerze:
wyłączenie płatnych providerów musi być wymuszone przez źródłowy worker.

Przed uruchomieniem wymagane są: wdrożony zgodny oryginalny worker,
osiągalna Ollama z modelem, skonfigurowane sekrety i dostępny manualny
workflow na właściwej gałęzi. Plik workflow w niescalonym PR nie uruchamia
agentów automatycznie. Obecnie nie potwierdzono żadnej produkcyjnej odpowiedzi.
