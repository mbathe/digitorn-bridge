import React from "react";
import ReactDOM from "react-dom/client";
import { PreviewProvider } from "./lib/preview-sdk";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <PreviewProvider>
      <App />
    </PreviewProvider>
  </React.StrictMode>,
);
