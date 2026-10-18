// Combinational ALU: add, subtract, and, or.
module alu #(
  parameter int WIDTH = 8
) (
  input  logic [1:0]       op,
  input  logic [WIDTH-1:0] a,
  input  logic [WIDTH-1:0] b,
  output logic [WIDTH-1:0] y
);
  always_comb begin
    unique case (op)
      2'd0:    y = a + b;
      2'd1:    y = a - b;
      2'd2:    y = a & b;
      default: y = a | b;
    endcase
  end
endmodule
