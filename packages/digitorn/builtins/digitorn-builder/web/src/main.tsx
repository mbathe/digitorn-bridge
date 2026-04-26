import React from "react";
import ReactDOM from "react-dom/client";
import { DigiPreview } from "@digitorn/preview-sdk";
import App from "./App";
import "./styles/globals.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DigiPreview>
      <App />
    </DigiPreview>
  </React.StrictMode>,
);
