import PromotionIntake from "@/components/PromotionIntake";
import PromotionList from "@/components/PromotionList";

export default function PromotionsPage() {
  return (
    <main className="container">
      <h1>Supplier promotions</h1>
      <PromotionList />
      <PromotionIntake />
    </main>
  );
}
