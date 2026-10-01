// One door onto every persisted run. The page holds no channel to a resolver
// process: it reads a run's files through the routes and follows its journal
// over the event stream, so a run that is moving, one parked overnight and
// one that finished last week are all reachable through the same page.
import { useCallback, useEffect, useRef, useState } from "react";
import type { JournalEntry, RunSummary, SupervisorState } from "../generated/views";
import { eventsUrl, parkRun, readRun, readRuns, readSelected, resumeRun, submitAnswers } from "./api";
import { Concerns } from "./Concerns";
import { Masthead, type Connection } from "./Masthead";
import { Questions } from "./Questions";
import { Rail } from "./Rail";
import { Failures, Review } from "./Review";
import { Trace } from "./Trace";
import type { Scope } from "./filters";

// A long run outlives what a page can hold, so the oldest entries are dropped
// rather than accumulated until the tab dies. The record on disk is complete
// either way — this bounds the reader, not the run.
const RETAINED_ENTRIES = 4000;

// Each record is applied directly, so no re-project-and-diff tick runs.
// A phase or concern move is folded in place because the event carries
// exactly what changed; anything derived from the mailbox — offers, the
// operator-facing status — is re-read, because the run does not author an
// offer and cannot have recorded it.
const REPROJECTS = ["question_asked", "answer_settled", "run_failed"];

function wantedRun(): string {
  return decodeURIComponent(window.location.hash.slice(1));
}

export function App() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [state, setState] = useState<SupervisorState | null>(null);
  const [entries, setEntries] = useState<JournalEntry[]>([]);
  const [scope, setScope] = useState<Scope>({ kind: "merged" });
  const [connection, setConnection] = useState<Connection>({ label: "connecting", tone: "" });
  const [lastEvent, setLastEvent] = useState("no events yet");
  const [selectable, setSelectable] = useState(false);
  const [questionsError, setQuestionsError] = useState("");
  const runId = state?.run_id ?? null;
  const current = useRef<string | null>(null);
  current.current = runId;

  const refreshRuns = useCallback(async () => {
    // The rail keeps what it had where the index cannot be read; the
    // connection pill reports the stream, and the stream reports itself.
    try {
      setRuns((await readRuns()).runs);
    } catch (error) {
      setConnection({ label: String(error), tone: "danger" });
    }
  }, []);

  const hydrate = useCallback(async () => {
    await refreshRuns();
    try {
      const fresh = current.current === null ? await readSelected() : await readRun(current.current);
      if (fresh === null) {
        setSelectable(true);
        setConnection({ label: "select a run", tone: "" });
        return;
      }
      setState(fresh);
    } catch (error) {
      setConnection({ label: String(error), tone: "danger" });
    }
  }, [refreshRuns]);

  const selectRun = useCallback(async (wanted: string) => {
    try {
      const fresh = await readRun(wanted);
      setEntries([]);
      setScope({ kind: "merged" });
      setLastEvent("no events yet");
      setQuestionsError("");
      setState(fresh);
      if (wantedRun() !== wanted) window.location.hash = encodeURIComponent(wanted);
    } catch (error) {
      setConnection({ label: String(error), tone: "danger" });
    }
  }, []);

  // The stream, one per open run: a reconnect resumes from the last sequence
  // the browser saw, and opening it re-reads the projection.
  useEffect(() => {
    if (runId === null) return;
    const source = new EventSource(eventsUrl(runId));
    let reprojection = 0;
    source.onopen = () => {
      setConnection({ label: "watching", tone: "ok" });
      void hydrate();
    };
    source.onerror = () => setConnection({ label: "reconnecting", tone: "danger" });
    source.onmessage = (message: MessageEvent<string>) => {
      const entry = JSON.parse(message.data) as JournalEntry;
      setEntries((held) => (held.length >= RETAINED_ENTRIES ? [...held.slice(1), entry] : [...held, entry]));
      setLastEvent(`last event ${new Date(entry.at).toLocaleTimeString()}`);
      const event = entry.event;
      if (event.type === "phase_changed") {
        setState((held) => (held === null ? null : { ...held, phase: event.phase }));
      }
      if (event.type === "concern_progressed") {
        setState((held) =>
          held === null
            ? null
            : {
                ...held,
                concerns: held.concerns.map((concern) =>
                  concern.id === event.progress.concern_id
                    ? { ...concern, status: event.progress.status, reason: event.progress.reason }
                    : concern,
                ),
              },
        );
      }
      // A replayed catch-up delivers a run's worth of these in one burst;
      // what the page needs afterwards is one reprojection of now.
      if (REPROJECTS.includes(event.type)) {
        window.clearTimeout(reprojection);
        reprojection = window.setTimeout(() => void hydrate(), 250);
      }
    };
    return () => {
      window.clearTimeout(reprojection);
      source.close();
    };
  }, [runId, hydrate]);

  useEffect(() => {
    const wanted = wantedRun();
    if (wanted !== "") {
      void selectRun(wanted);
    } else {
      void hydrate();
    }
    const changed = () => {
      const next = wantedRun();
      if (next !== "" && next !== current.current) void selectRun(next);
    };
    window.addEventListener("hashchange", changed);
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshRuns();
    }, 30000);
    return () => {
      window.removeEventListener("hashchange", changed);
      window.clearInterval(timer);
    };
  }, [selectRun, hydrate, refreshRuns]);

  async function decided(request: () => Promise<SupervisorState>): Promise<boolean> {
    setQuestionsError("");
    try {
      setState(await request());
      return true;
    } catch (error) {
      setQuestionsError(String(error));
      return false;
    }
  }

  function opened(id: string) {
    setScope({ kind: "concern", id });
    document.querySelector(".trace-wrap")?.scrollIntoView({ behavior: "smooth" });
  }

  return (
    <div className="app">
      <Rail runs={runs} current={runId} onSelect={(wanted) => void selectRun(wanted)} />
      <main className="stage">
        <Masthead state={state} connection={connection} lastEvent={lastEvent} />
        {state === null && selectable && (
          <section>
            <h2>Pick a run</h2>
            <p className="muted">
              Every run recorded under <span className="mono">.lup/resolve</span> is listed on the
              left — moving, parked, or finished, all through the same page. Start one with{" "}
              <span className="mono">uv run lup-devtools harness resolve</span>.
            </p>
          </section>
        )}
        {state !== null && (
          <>
            <Questions
              state={state}
              error={questionsError}
              onSubmit={(answers) => decided(() => submitAnswers(state.run_id, { answers }))}
              onPark={() => void decided(() => parkRun(state.run_id, { reason: "parked from the supervisor page" }))}
              onResume={() => void decided(() => resumeRun(state.run_id))}
            />
            <Concerns concerns={state.concerns} onOpen={opened} />
            <Trace
              key={state.run_id}
              runId={state.run_id}
              entries={entries}
              scope={scope}
              onScope={setScope}
              onEarlier={(page) => setEntries((held) => [...page, ...held])}
            />
            {state.review !== null && <Review review={state.review} />}
            <Failures failures={state.failures} />
          </>
        )}
        <footer>
          <span>{window.location.origin}</span>
        </footer>
      </main>
    </div>
  );
}
