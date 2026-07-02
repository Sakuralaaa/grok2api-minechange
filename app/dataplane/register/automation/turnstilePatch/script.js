function getRandomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

const screenX = getRandomInt(800, 1200);
const screenY = getRandomInt(400, 600);

Object.defineProperty(MouseEvent.prototype, "screenX", { value: screenX });
Object.defineProperty(MouseEvent.prototype, "screenY", { value: screenY });
