import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { paletteCss } from "./palette";
import "./styles.css";

// The palette's custom properties, generated from its table before the page draws with them.
const palette = document.createElement("style");
palette.id = "palette";
palette.textContent = paletteCss();
document.head.prepend(palette);

const root = document.getElementById("root");
if (root === null) throw new Error("the dashboard page carries no #root to mount into");
createRoot(root).render(<StrictMode><App /></StrictMode>);
