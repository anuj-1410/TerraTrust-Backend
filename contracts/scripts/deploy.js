const hre = require("hardhat");

async function waitForDeployment(contract) {
  if (typeof contract.waitForDeployment === "function") {
    await contract.waitForDeployment();
    return;
  }

  if (typeof contract.deployed === "function") {
    await contract.deployed();
  }
}

async function getContractAddress(contract) {
  if (typeof contract.getAddress === "function") {
    return contract.getAddress();
  }

  return contract.address;
}

async function main() {
  const [deployer] = await hre.ethers.getSigners();

  console.log("Deploying TerraTrustToken with account:", deployer.address);

  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log("Account balance:", balance.toString());

  const Token = await hre.ethers.getContractFactory("TerraTrustToken");
  const token = await Token.deploy();
  await waitForDeployment(token);

  const contractAddress = await getContractAddress(token);
  console.log("TerraTrustToken deployed to:", contractAddress);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});