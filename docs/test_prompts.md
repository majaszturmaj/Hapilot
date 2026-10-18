# Zestaw Promptrów Testowych do Systemu Hapilot

Poniższe prompty służą do weryfikacji naprawionego utrzymania kontekstu konwersacji (amnezja wsteczna) oraz kierowania powiązanych zapytań z chatu przez router/planistę.

Skopiuj odpowiednie bloki i wklej do agenta z poziomu interfejsu.

---

## 🧪 Test 1: Weryfikacja ciągłości konwersacyjnej w RAG (Pytania uściślające)
**Cel:** Sprawdzenie, czy agent zachowuje wiedzę o wyciągniętych dotąd informacjach (z procedur) podczas zadawania ogólnych pytań filtrujących ("follow-up").

**Krok 1:**
```text
Czego potrzebuję, by złożyć wniosek o urlop naukowy w naszej organizacji?
```

**Krok 2** (wyślij dopiero po odpowiedzi na Krok 1):
```text
A czy wspomniano o dodatkowych terminach dla stanowisk badawczych?
```

**Krok 3:**
```text
Ok, podsumuj to w dwóch bardzo krótkich punktach, proszę.
```
**Oczekiwany wynik:** W krokach 2 i 3 model LLM powinien zsyntetyzować odpowiedź na podstawie wcześniej pozyskanego tekstu o urlopie naukowym bez podawania komunikatu w stylu "Brak odpowiedzi, bo nie ma 'dodatkowych terminach' w dokumentach".

---

## 🧪 Test 2: Modyfikacja dokumentu w toku (Bez PDF-a - re-redakcja)
**Cel:** Upewnienie się, że router nie używa `rag_search` w celu wyszukania pojęć typu "napisz to prościej" i nie gubi kontekstu tworzonego pisma.

**Krok 1:**
```text
Zredaguj krótkiego maila do Działu Informatyki z prośbą o założenie nowego środowiska testowego na serwerze 19. Nie potrzebuję z tego PDF-a, tylko tekst.
```

**Krok 2:**
```text
Napisz to prościej i krócej, po żołniersku.
```

**Krok 3:**
```text
Dodałbyś informację o pośpiechu z powodu bliskiego terminu oddania grantu?
```
**Oczekiwany wynik:** Agent powinien wygenerować draft e-maila w Kroku 1, zwięźle skrócić go w Kroku 2 omijając zbędną dokumentację, a w Kroku 3 wkleić do istniejącego już w "Historii" zdania o pośpiechu. 

---

## 🧪 Test 3: Wznowienie rozmowy ("Czat" i planowanie)
**Cel:** Sprawdzenie, czy planista (STORM) nie nadinterpretuje krótkich wypowiedzi ani zgody/odmowy, a router zgrabnie prowadzi ciąg rozmowy bez błędów.

**Krok 1:**
```text
Jakie mamy procedury zgłaszania nowego projektu z funduszy europejskich w firmie?
```

**Krok 2** (po otrzymaniu odpowiedzi):
```text
Aha, rozumiem. Zastanawiam się, czy zgłosić pomysł o ekologicznych maszynach, ale dokumenty wydają się skomplikowane. Uważasz, że to ma sens? 
```

**Krok 3:**
```text
Nie, dzięki, to mi wystarczy :)
```
**Oczekiwany wynik:** Po odnalezieniu i zsyntetyzowaniu tekstu w Krok 1, Krok 2 to typowa konwersacja. W Kroku 3 agent nie wywołuje RAG'a dla "nie, dzięki", tylko uprzejmie się żegna lub wyłącza (korzystając z nowej instrukcji dla `llm_synthesis` / węzła czatu).

---

## 🧪 Test 4: Złożona kombinacja RAG + PDF + Modyfikacja (Poważne zadanie procesowe)
**Cel:** Test przepływu od szukania informacji, wygenerowania szablonu do edycji bezpośredniej na wygenerowanym LaTeXu.

**Krok 1:**
```text
Chcę napisać podanie o urlop. Sprawdź kto przyjmuje wnioski o urlop badawczy i przygotuj podanie w formie PDF-a używając moich danych wpisanych na krótko (Jan Kowalski, stanowisko inżynier).
```
*- Wymaga interwencji (zatwierdzenie parametrów formularza)*

**Krok 2** (po wygenerowaniu PDF):
```text
Zmień proszę datę dokumentu na 1 czerwca 2026 r, zmodyfikuj wygenerowany pdf.
```

**Krok 3:**
```text
Popraw też moje stanowisko na 'Główny Inżynier'.
```
**Oczekiwany wynik:** W Kroku 1 RAG szuka "kto przyjmuje wnioski", a następnie system woła szablon listu do zatwierdzenia przez formularz HITL i generuje PDF. W Krokach 2 i 3 system rozpoznaje `ZMODYFIKUJ DATĘ`/`POPRAW` i wrzuca do węzła modyfikującego dokument w Sandboxie (podziękowanie dla wzbogaconej tablicy `MODIFY_KEYWORDS`).

---

## 🧪 Test 5: Tryb luźnego czatu i pogawędki (Bypass Redaktora)
**Cel:** Sprawdzenie zaktualizowanego narzędzia `chat`. Moduł planisty powinien poprawnie zmapować luźne wtrącenie na węzeł `run_chat` zamiast `run_writer`, co oszczędza tokeny i uwalnia od sztywnego proceduralnego języka.

**Pojedynczy Prompt:**
```text
Hej! Jak tam ci dzisiaj mija dzień? Ja właśnie skończyłem analizować trudny model fizyczny i robię sobie kawę. Co tam u ciebie, systemie?
```
**Oczekiwany wynik:** Planista wybiera krok z narzędziem `chat`. Router przekazuje to do węzła CHAT (a nie REDAKTOR). System odpowiada naturalnie i zwięźle.

---

## 🧪 Test 6: Pojedyncze, precyzyjne strzały z ukrytym znaczeniem (Extrakcja szablonu z "szumu")
**Cel:** Przepuszczalność ulepszonego uodpornionego filtru szablonów w węźle `run_document_generator`. Mimo silnych wyrazów naukowych, system musi użyć logiki nakazującej formę `letter`.

**Pojedynczy Prompt A:**
```text
jestem Adam Nowak, napisz mi podanie o urlop maj w związku z niewykorzystaniem wczesniej urlopu
```

**Pojedynczy Prompt B:**
```text
Piszę pracę doktorską z termodynamiki i analizuję wyniki z ostatnich sprawozdań terenowych. Jednakże, dzisiaj potrzebuję żebyś przygotował dla mnie wniosek o urlop na przyszły tydzień.
```
**Oczekiwany wynik:** Mimo użycia w B słów "praca doktorska", "wyniki", "sprawozdania", obecność "wniosek" twardo wymusza podanie i kieruje sprawę do szablonu `letter`, a nie do `report`.

---

## 🧪 Test 7: Bardzo długi, wklejony blok tekstu (Streszczenie, kod, brudnopis)
**Cel:** Weryfikacja działania planisty i redaktora, przy wrzucaniu mu "ściany tekstu", gdzie użytkownik dorzuca własny kontekst lub e-maila przesłanego mu przez kogoś innego.

**Pojedynczy Prompt:**
```text
Cześć. Dostałem dzisiaj rano takiego maila od kierownictwa, zobacz sam:

"Szanowni Państwo, uprzejmie informuję, że w związku z audytem w dziale w przyszłym tygodniu, wszystkie laboratoria muszą zostać zamknięte w czwartek od godziny 10:00 do 14:00. Proszę o natychmiastowe zabezpieczenie aparatury pomiarowej. Dodatkowo proszę przesłać raporty z dotychczasowych prób uderzeniowych najpóźniej do środy rano do pani Ani z sekretariatu. Z poważaniem, Dyrekcja."

Zrób mi na podstawie tego szybko zwięzłego maila, którego wyślę mojemu zespołowi stażystów, tak żeby wiedzieli, co mają robić (są w labie uderzeniowym). Bądź stanowczy. Nie rób z tego PDF, tylko napisz w oknie czatu.
```
**Oczekiwany wynik:** Planista wykrywa cel (przygotowanie draftu tekstu bez poszukiwania w bazie wiedzy) i kieruje to do narzędzia `llm_synthesis` (Redaktor). Omija `template_pdf`. Redaktor pisze formalny odzew do zespołu.

---

## 🧪 Test 8: Odmowa pomocy w czacie w połowie planowania 
**Cel:** Test inteligentnego przerwania, gdy nie mamy danych źródłowych.

**Krok 1:**
```text
Potrzebuję napisać wniosek o sfinansowanie zakupu licencji JetBrains IDEA, podasz dokładnie podpunkt z regulaminu finansowego?
```

**Krok 2** (wyślij gdy RAG przyzna, że nie znalazł danych, bo RAG rzadko to zawiera):
```text
A dobra, nie było tematu, zapytam bezpośrednio w księgowości. Miłego dnia!
```
**Oczekiwany wynik:** Krok 1 spróbuje uderzyć RAG, który poinformuje, że nic z tych rzeczy nie ma. W Kroku 2 system nie wymusi procedury szukania podania o licencję w RAG, tylko zamknie dyskusję na czacie pożegnaniem.

---

## 🧪 Test 9: Rozbudowane zjawisko - Raport z badania inżynierskiego
**Cel:** Narzędzie `template_pdf` musi poprawnie użyć struktury "Raportu" (która ma np. dedykowane sekcje _Introduction_, _Scope_, _Analysis_, _Results_) i zintegrować w nich ściany tekstu, z pominięciem procedury "List/Podanie".

**Pojedynczy Długi Prompt:**
```text
Cześć, potrzebuję abyś natychmiast wygenerował obszerny raport z naszych ostatnich testów maszyny wytrzymałościowej typu 'KRAK-300'. Dokument ma trafić do Kierownika Projektu. 
Oto moje notatki i dane, które trzeba elegancko opakować w poszczególne sekcje:

Testy odbyły się 12 marca na głównej hali Działu Badań. Założeniem projektu było zbadanie naprężeń próbki kruszywa oporowego przy nacisku rzędu 250 kN. 
Jeśli chodzi o analizę: zauważyliśmy mikropęknięcia przy granicy 215 kN, co spowodowało drobne odchylenia na odczytach tensometrów. Całe środowisko pracy trzymało temperaturę w normie (około 21 stopni Celsjusza), nie było drgań rezonansowych, maszyna pracowała wzorowo. 
Z wyników jasno wynika, że nasza obecna mieszanka kruszywa jest trochę za słaba na obciążenia ciągłe docelowe ok 230 kN i musi zostać wzmocniona polimerami. Takie też dajemy rekomendacje w podsumowaniu i zgłaszamy potrzebę modyfikacji stopu.

Wygeneruj odpowiedni PDF jako Raport naukowy. Uzupełnij odpowiednio sekcje wstępu, zakresu, analizy oraz wniosków i wyników. Moje stanowisko to 'Główny Inżynier Testów', a sprawdzającym raport ma być 'Kierownik Projektu'. Uładź to trochę, by miało formalny wydźwięk inżynieryjny.
```
**Oczekiwany wynik:** Router wysyła żądanie generowania raportu. Narzędzie rozpoznaje hasła "Raport" oraz "Analiza" i formatuje JSON do postaci `template_id: report`, wypełniając wszystkie wymagane bloki tekstem inżynierskim.

---

## 🧪 Test 10: Ekstensywny protokół z narady projektowej
**Cel:** Ekstrakcja danych strukturalnych. LLM musi poprawnie rozpoznać datę, czas, uczestników, agendę oraz podjęte decyzje i wstrzyknąć to do szablonu "Protokół" (`template_id: protocol`).

**Pojedynczy Długi Prompt:**
```text
Systemie, proszę o sporządzenie protokołu z dzisiejszego spotkania zarządu i grupy roboczej, który wygenerujesz jako gotowy dokument do archiwum w postaci PDF.

Spotkanie odbyło się w sali konferencyjnej B2. Trwało od 09:00 do 11:30. Przewodniczył mu Dyrektor Techniczny, Piotr Testowy, a ja sporządziłem notatki - protokolant Jan Kowalski.
Omawialiśmy dwa główne punkty:
1. Budżet na modernizację serwerowni.
2. Szkolenia BHP dla nowych pracowników fizycznych.

W trakcie pierwszej części dyskusja była dość burzliwa. Pan Nowak z księgowości zgłaszał rezerwy finansowe na poziom 50 tysięcy, ale ostatecznie uzgodniliśmy (punkt decyzji!), że przesuniemy środki z puli reklamowej i dorzucimy kolejne 30 tys, zamykając budżet na 80 tysiącach złotych. 
W drugim temacie było krócej - szkolenia odbędą się w dwóch turach w kwietniu, poprowadzi je firma zewnętrzna BHP-Ekspert. Ustaliliśmy też konkretne zadanie dla Pani Kasi z kadr (punkt akcji) – musi ona do piątku przygotować pełną listę uczestników i rozesłać zaproszenia.

Przygotuj mi oficjalny protokół do druku zachowując ładne wypunktowania na decyzje i podjęte kroki (akcje).
```
**Oczekiwany wynik:** Ekstrakcja kluczowych meta-informacji: _Meeting Title, Date, Place, Secretary, Chairperson_. Budowa list LaTeX w strukturach jsona w parametrach `DECISIONS` oraz `ACTION_ITEMS`. Szablon mapowany jako `protocol`.
