# Debian 13 — niezależne Centrum AI 24/7

## Cel

Telefon / ChatGPT ↔ chroniony MCP + API + panel WWW ↔ Debian 13: Ollama, kolejka zadań, lokalny worker AI, prywatna pamięć oraz osobne narzędzie Codex. ChatGPT Work jest **opcjonalny** — żaden worker nie polega na aktywnej sesji ChatGPT ani na limitach ChatGPT Plus.

**Ważne:** To aplikacja uruchamiana NA Debianie 13, a nie gotowy system operacyjny z obrazem ISO. Wykorzystanie zasobów wymaga zawsze włączonego hosta i odpowiedniej pamięci RAM. Codespaces może być automatycznie zatrzymany.

## Uruchomienie na własnym stale włączonym serwerze Debian 13

1. Zainstaluj Docker Engine z Compose v2; włącz usługę Dockera zgodnie z polityką administracyjną hosta. Nigdy nie dawaj agentom samodzielnego roota.
2. W katalogu repozytorium na docelowej gałęzi uruchom:

   ```bash
   bash debian13/start-ai-session.sh
   ```

3. Skrypt generuje prywatny token operatora, uruchamia `ollama`, `ollama-model-init`, API, prywatny panel WWW i `debian13-ai-worker`. W domyślnym trybie usługi mają politykę `restart: unless-stopped`. Panel działa na **127.0.0.1:3000** — nie jest publicznym adresem internetowym.
4. Otwórz port 3000 jako prywatny port w Codespaces, albo skonfiguruj osobną warstwę HTTPS + kontrolę dostępu na własnym serwerze. Nie publikuj niechronionego portu HTTP do Internetu.
5. Otwórz **Centrum AI 24/7** → sprawdź rzeczywisty heartbeat i model. Następnie w **Work** utwórz zadanie, kliknij **Analizuj przez Ollamę**, po skończeniu kliknij **Pokaż raport**.
6. Sprawdź logi bez odczytywania sekretów:

   ```bash
   docker compose -f debian13/system/compose.yaml -f debian13/system/compose.ollama.yaml ps
   docker compose -f debian13/system/compose.yaml -f debian13/system/compose.ollama.yaml logs --tail 60 debian13-ai-worker
   ```

Przy pierwszym uruchomieniu model `llama3.2-vision:11b` musi się pobrać. Host o małej pamięci może nie obsłużyć modelu; zmień `OLLAMA_MODEL` na mniejszy model tekstowy, zachowując osobny model Vision `OLLAMA_VISION_MODEL`. Podczas pobierania nie zakładaj, że inferencja już działa.

## Bezpieczeństwo i realne możliwości

- Lokalny worker odpowiada na **zadania zaakceptowane przez operatora**, wysyła zapytanie wyłącznie do prywatnej Ollamy, zapisuje wynik w SQLite i publikuje heartbeat.
- Na awarię / restart żądanie w trakcie przetwarzania zmienia stan na **przerwane** i wymaga ponownej zgody. Nie ma samoczynnego nieograniczonego ponawiania, kosztów API ani fałszywych wyników.
- Worker nie wykonuje poleceń z wygenerowanej odpowiedzi, nie zmienia plików, nie scala PR, nie robi zakupów i nie nadaje uprawnień root. Raport to **propozycja**, a nie potwierdzenie implementacji kodu.
- **Codex to oddzielne Centrum Programowania.** Logowanie przez ChatGPT możliwe w CLI zgodnie z dostępnością planu, z limitami. Wysyłanie zadań programistycznych do uruchomienia przez Codex wymaga osobnej konfiguracji i autoryzacji; nie jest częścią lokalnej kolejki analiz.
- Kafelki Alex, Aria, Astyria i Aurelius są profilami lokalnymi. Połączenie z ich **oryginalnymi** niezależnymi procesami jest nadal osobnym punktem odbioru. Nie oznaczamy ich jako pracujących jedynie na podstawie obecności konfiguracji.
- ChatGPT Work można dodać później jako integrację z MCP. System nie zależy od limitów Work, ale usługi OpenAI/Codex oraz opcjonalny Gemini mają własne warunki i limity.
- Wtyczka Debian13 Connect korzysta z odczytowych narzędzi MCP. Aby łączyć się z nią przez ChatGPT na Androidzie, trzeba utworzyć autoryzowany endpoint HTTPS i zweryfikować rzeczywisty transport; localhost nie wystarczy.

## Potwierdzenia ukończenia

- [x] Kod kolejkowania zadań w SQLite; bezpieczne jawne zatwierdzenie analizy
- [x] Kod rzeczywistego lokalnego workera Ollama i heartbeat
- [x] Kod prywatnego kontenera API i GUI oraz trwałej pracy po zamknięciu terminala
- [x] Kod podglądu wyników i statusów w panelu oraz przez MCP
- [ ] Przejście testów CI na Debianie 13 i smoke-test uruchomienia kontenerów
- [ ] Realna inferencja na docelowym hoście i test Vision ze zdjęciem
- [ ] Potwierdzenie działania po restarcie hosta i połączenia HTTPS z telefonu
- [ ] Podłączenie oryginalnych czterech agentów i rzeczywiste programowanie przez osobny Codex
- [ ] Podłączenie wtyczki Debian13 Connect na koncie ChatGPT

Każdy procent postępu ma oznaczać jasno zdefiniowane, zweryfikowane kryteria, **nie** obietnicę gotowego systemu 24/7.
