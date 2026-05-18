import React from "react";
import { createRoot } from "react-dom/client";
import { DigiPreview } from "@digitorn/preview-sdk";

import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DigiPreview>
      <App />
    </DigiPreview>
  </React.StrictMode>,
);
