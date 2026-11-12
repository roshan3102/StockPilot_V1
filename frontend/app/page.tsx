import Chat from "@/components/Chat";

export default function Home() {
  return (
    <main className="container">
      <h1>StockPilot</h1>
      <p>AI-assisted restocking for independent retailers.</p>
      <Chat />
    </main>
  );
}
