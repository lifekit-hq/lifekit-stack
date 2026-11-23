export const PAGE = {
  url: "https://leetcode.com/problems/two-sum/description/",
  title: "Two Sum",
  problem: "Given an array of integers nums and an integer target, return indices of the two numbers that add up to target.",
  code: "class Solution:\n    def twoSum(self, nums, target):\n        seen = {}\n        for i, n in enumerate(nums):\n            if target - n in seen:\n                return [seen[target - n], i]\n            seen[n] = i\n",
  language: "python3",
};

export const TURN = { v: 1, input: { kind: "open" }, page: PAGE };

export const GOOD_REPLY = {
  text: "Look at the pointer after the second step. What does seen hold now?",
  visual: {
    html: '<svg viewBox="0 0 120 40" width="240"><rect x="2" y="8" width="30" height="24" fill="#eef" stroke="#336"/><text x="12" y="25" font-size="14">2</text></svg>',
  },
  expects: "tap",
  options: [
    { id: "a", label: "one entry" },
    { id: "b", label: "two entries" },
  ],
};

/** A gateway that answers with the given reply arguments, and remembers what it was asked. */
export function fakeGateway(args = GOOD_REPLY) {
  const calls = [];
  return {
    calls,
    async turn(prompt, key, extra) {
      calls.push({ prompt, key, extra });
      if (args instanceof Error) throw args;
      return typeof args === "function" ? args(prompt) : args;
    },
    async probe() {},
  };
}
