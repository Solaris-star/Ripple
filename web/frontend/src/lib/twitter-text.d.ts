declare module 'twitter-text' {
  const twitterText: {
    parseTweet(text: string): { weightedLength: number; valid: boolean };
    extractUrlsWithIndices(text: string): { url: string; indices: [number, number] }[];
    hasInvalidCharacters(text: string): boolean;
  };
  export default twitterText;
}
