import { Sparkles } from "lucide-react";

export default function EmptyState({ message }: { message?: string }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center text-center p-8 pointer-events-none">
      <div className="w-16 h-16 rounded-2xl bg-accent/10 border border-accent/20 flex items-center justify-center mb-5 animate-pulse">
        <Sparkles className="w-7 h-7 text-accent" />
      </div>
      <div className="text-base font-semibold text-ink mb-1.5">
        Waiting for the agent
      </div>
      <div className="text-sm text-ink-muted max-w-sm">
        {message ?? "The Builder agent will write your app.yaml. The graph will appear here automatically."}
      </div>
    </div>
  );
}
