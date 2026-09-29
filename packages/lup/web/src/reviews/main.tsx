import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) throw new Error("the review inbox page carries no #root to mount into");
createRoot(root).render(<StrictMode><App /></StrictMode>);
