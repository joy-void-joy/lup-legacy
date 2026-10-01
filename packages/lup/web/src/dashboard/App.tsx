// The operator's dashboard: one buffer in the middle, the agents tree on its
// left and the context on its right, the box under it, the tabline above and
// the statusline below; floats over it. Below 861 px it is one column, with the
// tree and the context as drawers and the actions under the thumb.
import { useEffect, useLayoutEffect, useState } from "react";
import { keyHandler } from "./actions";
import { Context } from "./Context";
import { Dashboard } from "./dashboard";
import { Editor } from "./EditorWindow";
import { Floats } from "./Floats";
import { CommandLine, Notices, Statusline, Tabline } from "./Shell";
import { useStore, type PageState } from "./state";
import { ActionBar, Sheet, TabBar, TopBar, useGestures } from "./Touch";
import { Left } from "./Tree";

function Denied({ d, state }: { d: Dashboard; state: PageState }) {
  return <main className="denied"><h1>lup dashboard · access denied</h1>
    <p>This browser is not authorized, or its dashboard session has expired. Open the dashboard with the operator's <code>uv run lup-devtools dashboard open</code>, or the launch link <code>dashboard serve</code> printed, then return to this request link.</p>
    {state.linked !== null && <p>Requested review: <code>{state.linked.id}</code></p>}
    {state.access.notice !== "" && <p className="notice" role="alert">{state.access.notice}</p>}
    <button type="button" className="btn" onClick={() => d.reconnect()}>Check access again</button>
  </main>;
}

function Setup({ d, state }: { d: Dashboard; state: PageState }) {
  const panes = state.panes;
  const pane = panes?.[state.setupAt] ?? panes?.[0];
  return <>
    <section id="w-setup-list" className={`win${state.focus === "setup" ? " focus" : ""}`} data-win="setup" aria-label="Repositories">
      <div className="wb"><span className="t">repositories</span></div>
      <div className="buf" tabIndex={-1} ref={(element) => { d.elements.setup = element; }}>
        {panes === null ? <p className="intro" role="status">Loading repositories…</p> : panes.length === 0 ? <p className="intro">No repository's setup is served here.</p>
          : panes.map((each, index) => <button key={each.key} type="button" className={`tr${pane?.key === each.key ? " sel" : ""}`} aria-pressed={pane?.key === each.key} title={each.repository} onClick={() => d.set({ setupAt: index })}>{each.name}</button>)}
      </div>
    </section>
    <section id="w-setup-frame" className="win" data-win="setupframe" aria-label="Setup">
      <div className="wb"><span className="t">setup{pane !== undefined ? ` · ${pane.name}` : ""}</span>{pane !== undefined && <span className="muted">{pane.repository}</span>}</div>
      {pane !== undefined && <iframe className="setup-frame" title={`Setup · ${pane.name}`} src={pane.path} />}
    </section>
  </>;
}

export function App() {
  const [d] = useState(() => new Dashboard());
  const state = useStore(d.store);
  useEffect(() => {
    d.start();
    return () => d.stop();
  }, [d]);
  useEffect(() => {
    const keys = keyHandler(d);
    const typing = () => {
      const active = document.activeElement;
      const now = active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement;
      if (d.state.typing !== now) d.set({ typing: now });
      if (active instanceof HTMLTextAreaElement && (active.id === "note" || active.id === "reply") && d.state.focus !== "composer") d.set({ focus: "composer" });
    };
    const focusOut = () => setTimeout(typing, 0);
    window.addEventListener("keydown", keys.down);
    window.addEventListener("keyup", keys.up);
    window.addEventListener("blur", keys.blur);
    document.addEventListener("focusin", typing);
    document.addEventListener("focusout", focusOut);
    document.addEventListener("click", focusOut, true);
    return () => {
      window.removeEventListener("keydown", keys.down);
      window.removeEventListener("keyup", keys.up);
      window.removeEventListener("blur", keys.blur);
      document.removeEventListener("focusin", typing);
      document.removeEventListener("focusout", focusOut);
      document.removeEventListener("click", focusOut, true);
    };
  }, [d]);
  useLayoutEffect(() => d.applyFocus());
  useLayoutEffect(() => {
    document.documentElement.style.setProperty("--fs", `${state.settings.size}px`);
    document.documentElement.style.setProperty("--lh", `${Math.round(state.settings.size * 1.4)}px`);
  }, [state.settings.size]);
  useGestures(d, state.narrow);
  if (state.denied) return <Denied d={d} state={state} />;
  const classes = [
    state.narrow ? "touch" : "",
    `focus-${state.focus}`,
    state.touch.drawer === "tree" ? "drawer-tree" : "",
    state.touch.drawer === "context" ? "drawer-context" : "",
    state.settings.wrap ? "" : "nowrap",
    state.settings.numbers ? "" : "nonumber",
  ].filter((each) => each !== "").join(" ");
  const mainClasses = [state.queueShown || state.narrow ? "" : "noqueue", state.contextShown || state.narrow ? "" : "nocontext"].filter((each) => each !== "").join(" ");
  return <div id="app" className={classes}>
    {state.narrow ? <TopBar d={d} state={state} /> : <Tabline d={d} state={state} />}
    <main id="main" data-view={state.view} className={mainClasses}>
      {state.view === "setup" ? <Setup d={d} state={state} /> : <>
        {(state.queueShown || state.narrow) && <Left d={d} state={state} />}
        <Editor d={d} state={state} />
        {(state.contextShown || state.narrow) && <Context d={d} state={state} />}
      </>}
    </main>
    {!state.narrow && <Statusline d={d} state={state} />}
    <CommandLine d={d} state={state} />
    {state.narrow && <ActionBar d={d} state={state} />}
    {state.narrow && <TabBar d={d} state={state} />}
    {state.narrow && (state.touch.drawer !== "" || state.touch.sheet !== "") && <div id="scrim" onClick={() => d.set({ touch: { drawer: "", sheet: "" }, float: state.float?.kind === "hover" ? null : state.float })} />}
    <Sheet d={d} state={state} />
    <Floats d={d} state={state} />
    <Notices d={d} state={state} />
  </div>;
}
