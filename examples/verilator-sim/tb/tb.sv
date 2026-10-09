// Testbench: drives the counter's enable for 200 cycles (always on for `smoke`, random
// otherwise) and checks the count. Plusargs: +test=<name> +seed=<n>.
module tb;
  logic       clk = 1'b0;
  logic       rst = 1'b1;
  logic       en = 1'b0;
  logic [7:0] count;
  int unsigned seed;
  int unsigned enables = 0;
  string      test;

  counter #(.WIDTH(8)) dut (.clk(clk), .rst(rst), .en(en), .count(count));

  always #5 clk = ~clk;

  initial begin
    if (!$value$plusargs("test=%s", test)) test = "smoke";
    if (!$value$plusargs("seed=%d", seed)) seed = 1;
    void'($urandom(seed));
    @(negedge clk) rst = 1'b0;
    repeat (200) begin
      en = (test == "smoke") ? 1'b1 : 1'($urandom_range(1));
      @(negedge clk);
      if (en) enables++;
    end
    $display("%s test=%0s seed=%0d enables=%0d", (count == enables[7:0]) ? "PASS" : "FAIL",
             test, seed, enables);
    $finish;
  end
endmodule
